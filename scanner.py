#!/usr/bin/env python3
"""
NSE 9:15 AM Intraday Scanner + Paper-Trade Tracker
===================================================
Scans liquid NSE stocks at market open using RSI, volume surge, trend
structure (SMA 20/50), opening gap, average daily range and the Nifty
index direction. Logs every signal and evaluates how it actually played
out, after realistic costs. Also analyzes any NSE stock on demand.

CLI:
  python scanner.py scan       -> run the scan, log signals, print table
  python scanner.py evaluate   -> paper-trade P&L report of past signals

Web UI:
  python app.py                -> http://localhost:5050

DISCLAIMER: screening + learning tool. It does NOT guarantee profit.
Paper trade for at least 30 sessions before risking real money.
yfinance data can be delayed ~1-15 min; for true realtime use a broker API.
"""

import csv
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

# ---------------- Config ----------------
# User-tunable settings live in config.json (editable from the web UI).
DEFAULT_CONFIG = {
    "capital": 200_000,          # portfolio size (INR)
    "risk_per_trade_pct": 0.5,   # % of capital risked per trade (max loss if SL hits)
    "stop_loss_pct": 1.0,        # stop loss distance from entry
    "target_pct": 1.5,           # profit target distance (1.5:1 reward:risk)
}
CONFIG_BOUNDS = {
    "capital": (10_000, 100_000_000),
    "risk_per_trade_pct": (0.05, 5.0),
    "stop_loss_pct": (0.2, 10.0),
    "target_pct": (0.3, 20.0),
}
MIN_AVG_RANGE_PCT = 1.5      # stock must move at least this much per day on average
MIN_SCORE = 5                # minimum score to qualify
MAX_SIGNALS = 5              # top N signals only
COST_PCT_ROUNDTRIP = 0.20    # brokerage + STT + charges + slippage (% of turnover)

IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "signals_log.csv")
LAST_SCAN_FILE = os.path.join(BASE_DIR, "last_scan.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
CSV_FIELDS = ["date", "symbol", "side", "score", "rsi", "vol_ratio", "gap_pct",
              "avg_range_pct", "entry", "sl", "target", "qty", "status",
              "exit_price", "pnl"]

# NSE trading holidays 2026 (best effort; lunar-calendar dates are approximate).
# During market hours the live "did Nifty print a candle today" check is the
# authority — this list mainly covers pre-open and after-hours display.
NSE_HOLIDAYS = {
    "2026-01-26": "Republic Day",
    "2026-03-04": "Holi",
    "2026-03-26": "Ram Navami",
    "2026-03-31": "Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-27": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-09": "Diwali Balipratipada",
    "2026-11-24": "Guru Nanak Jayanti",
    "2026-12-25": "Christmas",
}

# Fallback list (~200 liquid names) used only if the full NSE equity list
# can't be downloaded. The real universe is ALL listed NSE equities (~2000),
# fetched from NSE's official archive and cached for a week.
FALLBACK_UNIVERSE = [
    # Nifty 50 core
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "HINDUNILVR", "ITC",
    "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK", "ASIANPAINT", "MARUTI",
    "SUNPHARMA", "TITAN", "ULTRACEMCO", "BAJFINANCE", "NESTLEIND", "WIPRO",
    "M&M", "NTPC", "POWERGRID", "TATASTEEL", "ADANIENT",
    "ADANIPORTS", "COALINDIA", "BAJAJFINSV", "HCLTECH", "JSWSTEEL",
    "INDUSINDBK", "HINDALCO", "DRREDDY", "CIPLA", "TECHM", "GRASIM",
    "BRITANNIA", "EICHERMOT", "HEROMOTOCO", "APOLLOHOSP", "BPCL", "ONGC",
    "SBILIFE", "HDFCLIFE", "TATACONSUM", "TRENT", "BEL", "HAL",
    "BAJAJ-AUTO", "SHRIRAMFIN", "JIOFIN",
    # Banks / financials
    "PNB", "BANKBARODA", "CANBK", "IDFCFIRSTB", "FEDERALBNK", "YESBANK",
    "RBLBANK", "AUBANK", "BANDHANBNK", "UNIONBANK", "INDIANB", "IDBI",
    "CHOLAFIN", "MUTHOOTFIN", "MANAPPURAM", "LICHSGFIN", "PNBHOUSING",
    "PFC", "RECLTD", "IREDA", "HUDCO", "LICI", "SBICARD", "HDFCAMC",
    "ANGELONE", "BSE", "CDSL", "CAMS", "MCX", "POLICYBZR", "ABCAPITAL",
    "POONAWALLA", "360ONE", "MOTILALOFS", "IIFL", "NUVAMA",
    # IT / tech
    "LTIM", "MPHASIS", "COFORGE", "PERSISTENT", "OFSS", "TATAELXSI",
    "KPITTECH", "CYIENT", "BIRLASOFT", "TATATECH", "LTTS", "NAUKRI",
    "TANLA", "ROUTE", "INTELLECT", "BSOFT", "KFINTECH",
    # Auto / ancillaries
    "TVSMOTOR", "ASHOKLEY", "BHARATFORG", "MOTHERSON", "BOSCHLTD",
    "EXIDEIND", "MRF", "APOLLOTYRE", "CEAT", "JKTYRE", "BALKRISIND",
    "ESCORTS", "OLECTRA", "TIINDIA", "SONACOMS", "UNOMINDA",
    # Pharma / health
    "ZYDUSLIFE", "LUPIN", "AUROPHARMA", "GLENMARK", "BIOCON", "ALKEM",
    "TORNTPHARM", "DIVISLAB", "MANKIND", "SYNGENE", "LAURUSLABS",
    "GRANULES", "NATCOPHARM", "AJANTPHARM", "IPCALAB", "MAXHEALTH",
    "FORTIS", "LALPATHLAB", "METROPOLIS",
    # Metals / energy / power
    "VEDL", "SAIL", "NMDC", "NATIONALUM", "JINDALSTEL", "JSL",
    "APLAPOLLO", "ADANIPOWER", "ADANIGREEN", "ADANIENSOL", "ATGL",
    "TATAPOWER", "JSWENERGY", "NHPC", "SJVN", "TORNTPOWER", "CESC",
    "IOC", "HINDPETRO", "GAIL", "PETRONET", "OIL", "MGL", "IGL",
    "HINDCOPPER", "MOIL", "SUZLON", "INOXWIND",
    # Infra / realty / rail / defence
    "IRCTC", "IRFC", "RVNL", "RITES", "RAILTEL", "IRCON", "CONCOR",
    "TITAGARH", "NCC", "NBCC", "IRB", "HFCL", "DLF", "OBEROIRLTY",
    "GODREJPROP", "PRESTIGE", "PHOENIXLTD", "BRIGADE", "LODHA", "SOBHA",
    "ANANTRAJ", "BDL", "MAZDOCK", "COCHINSHIP", "GRSE", "DATAPATTNS",
    "ZENTEC", "JSWINFRA",
    # Consumer / retail / food
    "DABUR", "MARICO", "GODREJCP", "COLPAL", "EMAMILTD", "VBL", "UBL",
    "RADICO", "BATAINDIA", "RELAXO", "PAGEIND", "ABFRL", "DMART",
    "NYKAA", "MANYAVAR", "KALYANKJIL", "SENCO", "JUBLFOOD", "DEVYANI",
    "SAPPHIRE", "WESTLIFE", "ETERNAL", "SWIGGY", "DELHIVERY", "PAYTM",
    # Industrials / capital goods
    "SIEMENS", "ABB", "CGPOWER", "BHEL", "THERMAX", "CUMMINSIND",
    "POLYCAB", "KEI", "HAVELLS", "CROMPTON", "VOLTAS", "BLUESTARCO",
    "DIXON", "AMBER", "KAYNES", "SYRMA", "ASTRAL", "SUPREMEIND",
    # Chemicals / cement / misc
    "PIIND", "UPL", "SRF", "DEEPAKNTR", "AARTIIND", "GNFC",
    "CHAMBLFERT", "COROMANDEL", "PIDILITIND", "BERGEPAINT", "KANSAINER",
    "SHREECEM", "AMBUJACEM", "ACC", "DALBHARAT", "JKCEMENT", "RAMCOCEM",
    "INDIACEM", "INDIGO", "TATACOMM", "IDEA", "INDUSTOWER", "SUNTV",
    "ZEEL", "PVRINOX", "NAZARA", "WELCORP", "WELSPUNLIV", "GPIL",
]

NSE_EQUITY_CSV = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
BSE_LIST_URL = ("https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
                "?Group=&Scripcode=&industry=&segment=Equity&status=Active")
EQUITY_LIST_FILE = os.path.join(BASE_DIR, "equity_universe.json")
EQUITY_LIST_MAX_AGE_DAYS = 7
MIN_TURNOVER_CR_SCAN = 5     # ignore illiquid names in the 9:15 scan
MIN_TURNOVER_CR_MOVERS = 2   # ignore illiquid names in top movers

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
_SYM_RE = re.compile(r"[A-Z0-9&\-]{1,20}")


def _fetch_nse():
    """All NSE EQ-series equities -> (symbols, isins)."""
    import csv as _csv
    import io
    import urllib.request
    req = urllib.request.Request(NSE_EQUITY_CSV, headers=_UA)
    text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
    symbols, isins = [], set()
    for row in _csv.DictReader(io.StringIO(text)):
        row = {k.strip(): (v or "").strip() for k, v in row.items()}
        if row.get("SERIES") == "EQ" and _SYM_RE.fullmatch(row.get("SYMBOL", "")):
            symbols.append(row["SYMBOL"])
            if row.get("ISIN NUMBER"):
                isins.add(row["ISIN NUMBER"])
    return sorted(symbols), isins


def _fetch_bse(nse_isins):
    """Active BSE equities NOT dual-listed on NSE (dedup by ISIN) -> symbols."""
    import urllib.request
    req = urllib.request.Request(BSE_LIST_URL, headers={
        **_UA, "Referer": "https://www.bseindia.com/corporates/List_Scrips.html",
        "Accept": "application/json"})
    rows = json.loads(urllib.request.urlopen(req, timeout=45).read().decode())
    symbols = []
    for row in rows:
        isin = (row.get("ISIN_NUMBER") or "").strip()
        sid = (row.get("scrip_id") or "").strip().upper()
        if isin and isin in nse_isins:
            continue  # dual-listed — NSE side is more liquid, already covered
        if _SYM_RE.fullmatch(sid):
            symbols.append(sid)
    return sorted(set(symbols))


def universe_info(force=False):
    """{"nse": [...], "bse": [...]} — every listed Indian equity, cached weekly."""
    now_ts = datetime.now(IST).timestamp()
    if not force and os.path.exists(EQUITY_LIST_FILE):
        try:
            cached = json.load(open(EQUITY_LIST_FILE))
            age_days = (now_ts - cached.get("ts", 0)) / 86400
            if age_days < EQUITY_LIST_MAX_AGE_DAYS and len(cached.get("nse", [])) > 500:
                return cached
        except (json.JSONDecodeError, OSError):
            pass
    nse, bse = FALLBACK_UNIVERSE, []
    try:
        nse, nse_isins = _fetch_nse()
        try:
            bse = _fetch_bse(nse_isins)
        except Exception:
            bse = []
        info = {"ts": now_ts, "nse": nse, "bse": bse}
        with open(EQUITY_LIST_FILE, "w") as f:
            json.dump(info, f)
        return info
    except Exception:
        return {"ts": now_ts, "nse": nse, "bse": bse}


def full_universe(force=False):
    """Display symbols across both exchanges (NSE first, then BSE-only)."""
    info = universe_info(force)
    return info["nse"] + info["bse"]


def full_tickers():
    """Yahoo tickers for the whole Indian market: SYM.NS + SYM.BO."""
    info = universe_info()
    return [s + ".NS" for s in info["nse"]] + [s + ".BO" for s in info["bse"]]


_sets_cache = {"nse": None, "bse": None}


def yahoo_symbol(sym):
    """Map a display symbol to its Yahoo ticker (.NS preferred, .BO if BSE-only)."""
    if _sets_cache["nse"] is None:
        info = universe_info()
        _sets_cache["nse"] = set(info["nse"])
        _sets_cache["bse"] = set(info["bse"])
    if sym in _sets_cache["nse"]:
        return sym + ".NS"
    if sym in _sets_cache["bse"]:
        return sym + ".BO"
    return sym + ".NS"


def _download_universe(tickers, period):
    """Chunked yfinance download for a big ticker list -> {ticker: DataFrame}."""
    frames = {}
    chunk_size = 400
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i + chunk_size]
        try:
            data = yf.download(chunk, period=period, interval="1d",
                               group_by="ticker", auto_adjust=True,
                               progress=False, threads=True)
        except Exception:
            continue
        for t in chunk:
            try:
                df = data[t].dropna()
            except KeyError:
                continue
            if len(df) >= 60:
                frames[t] = df
    return frames


# Chart timeframes: range key -> (yfinance period, interval, label format)
CHART_RANGES = {
    "1D": ("1d", "5m", "%H:%M"),
    "1W": ("5d", "15m", "%d %b %H:%M"),
    "1M": ("1mo", "60m", "%d %b"),
    "3M": ("3mo", "1d", "%d %b"),
    "1Y": ("1y", "1d", "%b %y"),
    "5Y": ("5y", "1wk", "%b %y"),
}
MOVERS_FILE = os.path.join(BASE_DIR, "top_movers.json")
MOVERS_CACHE_MIN = 10


# ---------------- Config store ----------------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            saved = json.load(open(CONFIG_FILE))
            cfg.update({k: v for k, v in saved.items() if k in DEFAULT_CONFIG})
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save_config(updates):
    cfg = load_config()
    for key, (lo, hi) in CONFIG_BOUNDS.items():
        if key not in updates:
            continue
        try:
            val = float(updates[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number")
        if not lo <= val <= hi:
            raise ValueError(f"{key} must be between {lo:g} and {hi:g}")
        cfg[key] = int(val) if key == "capital" else round(val, 2)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    return cfg


# ---------------- Market status ----------------

_traded_cache = {"ts": None, "traded": False}


def _traded_today(now):
    """Did Nifty print a candle today? Cached 5 min so status polls stay cheap."""
    if _traded_cache["ts"] and (now - _traded_cache["ts"]).total_seconds() < 300:
        return _traded_cache["traded"]
    try:
        idx = flatten(yf.download("^NSEI", period="5d", interval="1d",
                                  auto_adjust=True, progress=False))
        traded = (not idx.empty) and idx.index[-1].date() == now.date()
    except Exception:
        return _traded_cache["traded"]
    _traded_cache.update(ts=now, traded=traded)
    return traded


def market_status():
    """NSE market state right now (IST): open / pre-open / closed / holiday."""
    now = datetime.now(IST)
    date_str = now.strftime("%Y-%m-%d")
    minutes = now.hour * 60 + now.minute
    open_min, close_min = 9 * 60 + 15, 15 * 60 + 30
    base = {"now_ist": now.strftime("%a, %d %b %Y %H:%M IST")}

    if now.weekday() >= 5:
        day = "Saturday" if now.weekday() == 5 else "Sunday"
        return {**base, "state": "closed", "label": "Market Closed",
                "detail": f"{day} — next session Monday 9:15 AM"}
    if date_str in NSE_HOLIDAYS:
        return {**base, "state": "holiday", "label": "Market Closed",
                "detail": f"Trading holiday: {NSE_HOLIDAYS[date_str]}"}
    if minutes < 9 * 60:
        return {**base, "state": "closed", "label": "Market Closed",
                "detail": "Opens today 9:15 AM"}
    if minutes < open_min:
        return {**base, "state": "pre", "label": "Pre-Open Session",
                "detail": "Continuous trading starts 9:15 AM"}
    if minutes <= close_min:
        # give Yahoo ~10 min after open to start printing today's candle
        if minutes >= open_min + 10 and not _traded_today(now):
            return {**base, "state": "holiday", "label": "Market Closed",
                    "detail": "No trades printed today — likely an unlisted holiday"}
        return {**base, "state": "open", "label": "Market Open",
                "detail": "Closes 3:30 PM"}
    if not _traded_today(now):
        return {**base, "state": "holiday", "label": "Market Closed",
                "detail": "No trades today — trading holiday"}
    return {**base, "state": "closed", "label": "Market Closed",
            "detail": "Closed 3:30 PM — session done for today"}


# ---------------- Indicators & scoring ----------------

def flatten(df):
    """yfinance sometimes returns MultiIndex columns even for one ticker."""
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    return df


def wilder_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def nifty_state():
    idx = flatten(yf.download("^NSEI", period="3mo", interval="1d",
                              auto_adjust=True, progress=False))
    close = idx["Close"].dropna()
    sma20 = close.rolling(20).mean()
    last, prev = float(close.iloc[-1]), float(close.iloc[-2])
    up = last > prev and last > float(sma20.iloc[-1])
    return {
        "level": round(last, 1),
        "change_pct": round((last / prev - 1) * 100, 2),
        "trend": "UP" if up else "DOWN",
        "spark": [round(v, 1) for v in close.tail(30)],
    }


def metrics_and_score(df, today, nifty_up):
    """All indicators + long/short scores for one stock. None if unusable.

    Uses only completed sessions for indicators; if today's candle exists,
    its open is the gap/entry reference.
    """
    live = df.index[-1].normalize() == today
    if live:
        today_open = float(df["Open"].iloc[-1])
        hist = df.iloc[:-1]
    else:
        today_open = None
        hist = df
    if len(hist) < 55:
        return None

    close = hist["Close"]
    prev_close = float(close.iloc[-1])
    day_change = (prev_close / float(close.iloc[-2]) - 1) * 100
    rsi = float(wilder_rsi(close).iloc[-1])
    vol_avg = float(hist["Volume"].rolling(20).mean().iloc[-1])
    vol_ratio = float(hist["Volume"].iloc[-1]) / vol_avg if vol_avg else float("nan")
    sma20 = float(close.rolling(20).mean().iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1])
    avg_range = float(((hist["High"] - hist["Low"]) / hist["Close"]).tail(14).mean() * 100)
    gap = (today_open / prev_close - 1) * 100 if today_open else 0.0
    if any(math.isnan(v) for v in (rsi, vol_ratio, sma20, sma50, avg_range)):
        return None

    long_score, long_why = 0, []
    if 52 <= rsi <= 72:
        long_score += 2; long_why.append(f"RSI {rsi:.0f} in momentum zone")
    if vol_ratio >= 1.5:
        long_score += 2; long_why.append(f"Volume {vol_ratio:.1f}x above average")
    if vol_ratio >= 2.5:
        long_score += 1
    if prev_close > sma20 > sma50:
        long_score += 2; long_why.append("Price above rising 20/50 DMA")
    if live and 0.3 <= gap <= 2.5:
        long_score += 2; long_why.append(f"Healthy gap up {gap:+.1f}%")
    if nifty_up:
        long_score += 1; long_why.append("Nifty in uptrend")
    if live and gap > 4:
        long_score = 0  # gap too big, chase risk

    short_score, short_why = 0, []
    if 28 <= rsi <= 45:
        short_score += 2; short_why.append(f"RSI {rsi:.0f} shows weakness")
    if vol_ratio >= 1.5:
        short_score += 2; short_why.append(f"Volume {vol_ratio:.1f}x above average")
    if vol_ratio >= 2.5:
        short_score += 1
    if prev_close < sma20 < sma50:
        short_score += 2; short_why.append("Price below falling 20/50 DMA")
    if live and -2.5 <= gap <= -0.3:
        short_score += 2; short_why.append(f"Gap down {gap:+.1f}%")
    if not nifty_up:
        short_score += 1; short_why.append("Nifty in downtrend")
    if live and gap < -4:
        short_score = 0

    if long_score >= short_score:
        side, score, why = "LONG", long_score, long_why
    else:
        side, score, why = "SHORT", short_score, short_why

    return {
        "live": live, "entry": today_open if live else prev_close,
        "prev_close": round(prev_close, 2), "day_change_pct": round(day_change, 2),
        "rsi": round(rsi, 1), "vol_ratio": round(vol_ratio, 2),
        "sma20": round(sma20, 2), "sma50": round(sma50, 2),
        "avg_range_pct": round(avg_range, 2), "gap_pct": round(gap, 2),
        "side": side, "score": score, "reasons": why,
        "long_score": long_score, "short_score": short_score,
        "spark": [round(v, 2) for v in close.tail(30)],
    }


def position_size(entry, cfg):
    risk_amount = cfg["capital"] * cfg["risk_per_trade_pct"] / 100.0
    per_share_risk = entry * cfg["stop_loss_pct"] / 100.0
    qty = int(risk_amount / per_share_risk) if per_share_risk > 0 else 0
    # no leverage assumed: cap position value at an equal slice of capital
    max_value = cfg["capital"] / MAX_SIGNALS
    if qty * entry > max_value:
        qty = int(max_value / entry)
    return qty


def _levels(entry, side, cfg):
    if side == "LONG":
        sl = entry * (1 - cfg["stop_loss_pct"] / 100)
        target = entry * (1 + cfg["target_pct"] / 100)
    else:
        sl = entry * (1 + cfg["stop_loss_pct"] / 100)
        target = entry * (1 - cfg["target_pct"] / 100)
    return round(sl, 2), round(target, 2)


# ---------------- Scan ----------------

def run_scan(log=True):
    cfg = load_config()
    now = datetime.now(IST)
    today = pd.Timestamp(now.date())
    nifty = nifty_state()
    up = nifty["trend"] == "UP"

    tickers = full_tickers()
    frames = _download_universe(tickers, "6mo")

    candidates = []
    any_live = False
    for tick, df in frames.items():
        sym = tick.rsplit(".", 1)[0]
        # liquidity gate: skip names where yesterday's turnover was too thin
        turnover_cr = float(df["Close"].iloc[-1]) * float(df["Volume"].iloc[-1]) / 1e7
        if turnover_cr < MIN_TURNOVER_CR_SCAN:
            continue
        m = metrics_and_score(df, today, up)
        if m is None:
            continue
        if m["live"]:
            any_live = True
        if m["avg_range_pct"] < MIN_AVG_RANGE_PCT or m["score"] < MIN_SCORE:
            continue
        entry = round(m["entry"], 2)
        sl, target = _levels(entry, m["side"], cfg)
        qty = position_size(entry, cfg)
        if qty == 0:
            continue
        candidates.append({
            "date": now.strftime("%Y-%m-%d"), "symbol": sym, "side": m["side"],
            "score": m["score"], "rsi": m["rsi"], "vol_ratio": m["vol_ratio"],
            "gap_pct": m["gap_pct"], "avg_range_pct": m["avg_range_pct"],
            "entry": entry, "sl": sl, "target": target, "qty": qty,
            "status": "OPEN", "exit_price": "", "pnl": "",
            "day_change_pct": m["day_change_pct"],
            "reasons": m["reasons"], "spark": m["spark"],
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    picks = candidates[:MAX_SIGNALS]

    result = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "live": any_live,
        "config": {**cfg, "cost_pct": COST_PCT_ROUNDTRIP, "universe_size": len(tickers)},
        "nifty": nifty,
        "picks": picks,
    }
    if log and picks:
        result["logged"] = _log_signals(picks)
    with open(LAST_SCAN_FILE, "w") as f:
        json.dump(result, f)
    return result


def _log_signals(picks):
    """Append picks to the CSV log, skipping same-day duplicates."""
    existing = set()
    if os.path.exists(LOG_FILE):
        for row in csv.DictReader(open(LOG_FILE)):
            existing.add((row["date"], row["symbol"]))
    new_rows = [{k: p[k] for k in CSV_FIELDS} for p in picks
                if (p["date"], p["symbol"]) not in existing]
    if new_rows:
        file_exists = os.path.exists(LOG_FILE)
        with open(LOG_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if not file_exists:
                writer.writeheader()
            writer.writerows(new_rows)
    return len(new_rows)


# ---------------- Single-stock lookup ----------------

def analyze_stock(symbol):
    """Full snapshot of any NSE stock: price, chart, indicators, scanner verdict."""
    sym = symbol.strip().upper().removesuffix(".NS")
    if not re.fullmatch(r"[A-Z0-9&\-]{1,20}", sym):
        raise ValueError("Invalid symbol")
    now = datetime.now(IST)
    today = pd.Timestamp(now.date())

    df = flatten(yf.download(yahoo_symbol(sym), period="1y", interval="1d",
                             auto_adjust=True, progress=False)).dropna()
    if (df.empty or len(df) < 60) and not yahoo_symbol(sym).endswith(".BO"):
        df = flatten(yf.download(sym + ".BO", period="1y", interval="1d",
                                 auto_adjust=True, progress=False)).dropna()
    if df.empty or len(df) < 60:
        raise ValueError(f"No NSE/BSE data for '{sym}' — check the symbol spelling")

    nifty = nifty_state()
    m = metrics_and_score(df, today, nifty["trend"] == "UP")
    if m is None:
        raise ValueError(f"Not enough history for '{sym}'")

    cfg = load_config()
    entry = round(m["entry"], 2)
    sl, target = _levels(entry, m["side"], cfg)
    qty = position_size(entry, cfg)

    last_close = float(df["Close"].iloc[-1])
    ref = m["prev_close"] if m["live"] else float(df["Close"].iloc[-2])
    tail = df.tail(90)
    chart = [{"d": idx.strftime("%d %b"), "c": round(float(row["Close"]), 2)}
             for idx, row in tail.iterrows()]

    return {
        "symbol": sym,
        "as_of": df.index[-1].strftime("%d %b %Y") + (" (today)" if m["live"] else " (last session)"),
        "price": round(last_close, 2),
        "change_pct": round((last_close / ref - 1) * 100, 2),
        "year_high": round(float(df["High"].max()), 2),
        "year_low": round(float(df["Low"].min()), 2),
        "metrics": m,
        "plan": {"entry": entry, "sl": sl, "target": target, "qty": qty,
                 "qualifies": bool(m["score"] >= MIN_SCORE and m["avg_range_pct"] >= MIN_AVG_RANGE_PCT)},
        "chart": chart,
        "nifty_trend": nifty["trend"],
    }


# ---------------- Charts (multi-timeframe) ----------------

def _clean_symbol(symbol):
    sym = symbol.strip().upper().removesuffix(".NS")
    if not re.fullmatch(r"[A-Z0-9&\-]{1,20}", sym):
        raise ValueError("Invalid symbol")
    return sym


def chart_data(symbol, rng="3M"):
    """Price series for one stock at a given timeframe (1D/1W/1M/3M/1Y/5Y)."""
    sym = _clean_symbol(symbol)
    if rng not in CHART_RANGES:
        raise ValueError(f"Unknown range '{rng}' — use one of {list(CHART_RANGES)}")
    period, interval, fmt = CHART_RANGES[rng]
    df = flatten(yf.download(yahoo_symbol(sym), period=period, interval=interval,
                             auto_adjust=True, progress=False)).dropna()
    if df.empty and not yahoo_symbol(sym).endswith(".BO"):
        df = flatten(yf.download(sym + ".BO", period=period, interval=interval,
                                 auto_adjust=True, progress=False)).dropna()
    if df.empty:
        raise ValueError(f"No data for '{sym}' at range {rng}")
    # intraday timestamps come in UTC; show them in IST
    idx = df.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert(IST)
    points = [{"t": ts.strftime(fmt), "c": round(float(c), 2)}
              for ts, c in zip(idx, df["Close"]) if not math.isnan(float(c))]
    first, last = points[0]["c"], points[-1]["c"]
    return {
        "symbol": sym, "range": rng, "interval": interval,
        "points": points,
        "change_pct": round((last / first - 1) * 100, 2) if first else 0,
        "high": round(float(df["High"].max()), 2),
        "low": round(float(df["Low"].min()), 2),
    }


# ---------------- Top movers ----------------

def top_movers(force=False):
    """Rank the whole universe by activity: volume surge, move size, RSI
    extremes, closeness to 52w high/low, turnover. Cached ~10 min."""
    now = datetime.now(IST)
    if not force and os.path.exists(MOVERS_FILE):
        try:
            cached = json.load(open(MOVERS_FILE))
            age_min = (now.timestamp() - cached.get("ts", 0)) / 60
            if age_min < MOVERS_CACHE_MIN:
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    tickers = full_tickers()
    frames = _download_universe(tickers, "1y")
    rows = []
    for tick, df in frames.items():
        sym = tick.rsplit(".", 1)[0]
        exch = "BSE" if tick.endswith(".BO") else "NSE"
        close = df["Close"]
        last = float(close.iloc[-1])
        prev = float(close.iloc[-2])
        chg = (last / prev - 1) * 100
        rsi = float(wilder_rsi(close).iloc[-1])
        vol_avg = float(df["Volume"].rolling(20).mean().iloc[-1])
        vol_ratio = float(df["Volume"].iloc[-1]) / vol_avg if vol_avg else 0
        avg_range = float(((df["High"] - df["Low"]) / df["Close"]).tail(14).mean() * 100)
        turnover_cr = last * float(df["Volume"].iloc[-1]) / 1e7
        if turnover_cr < MIN_TURNOVER_CR_MOVERS:
            continue  # illiquid — a 5% move on no volume is untradeable
        hi52 = float(df["High"].max())
        lo52 = float(df["Low"].min())
        from_high = (last / hi52 - 1) * 100
        if any(math.isnan(v) for v in (rsi, vol_ratio, avg_range)):
            continue

        score = (
            min(vol_ratio, 6) * 2.5
            + min(abs(chg), 10) * 1.5
            + min(avg_range, 6)
            + (2 if rsi >= 70 or rsi <= 30 else 0)
            + (2 if from_high > -3 else 0)          # near 52w high (breakout zone)
            + (2 if last / lo52 - 1 < 0.03 else 0)  # near 52w low (capitulation)
            + min(turnover_cr / 500, 3)             # liquidity weight
        )
        if chg > 0 and rsi >= 50:
            bias = "LONG"
        elif chg < 0 and rsi <= 50:
            bias = "SHORT"
        else:
            bias = "MIXED"
        rows.append({
            "symbol": sym, "exch": exch, "price": round(last, 2), "chg_pct": round(chg, 2),
            "vol_ratio": round(vol_ratio, 2), "rsi": round(rsi, 1),
            "range_pct": round(avg_range, 2), "turnover_cr": round(turnover_cr, 1),
            "from_high_pct": round(from_high, 1), "score": round(score, 1),
            "bias": bias, "spark": [round(v, 2) for v in close.tail(20)],
        })

    rows.sort(key=lambda r: r["score"], reverse=True)
    result = {
        "ts": now.timestamp(),
        "generated_at": now.strftime("%d %b %Y %H:%M"),
        "scanned": len(rows),
        "universe": len(tickers),
        "movers": rows[:60],
    }
    with open(MOVERS_FILE, "w") as f:
        json.dump(result, f)
    return result


# ---------------- News ----------------

def stock_news(symbol):
    """Recent Yahoo Finance news for one stock (title, publisher, link, time)."""
    sym = _clean_symbol(symbol)
    try:
        raw = yf.Ticker(yahoo_symbol(sym)).news or []
    except Exception:
        raw = []
    items = []
    for item in raw[:10]:
        # yfinance has two news formats (old flat, new nested under "content")
        content = item.get("content", item)
        title = content.get("title")
        if not title:
            continue
        link = (content.get("canonicalUrl") or {}).get("url") or content.get("link", "")
        publisher = (content.get("provider") or {}).get("displayName") or content.get("publisher", "")
        when = content.get("pubDate") or ""
        if not when and content.get("providerPublishTime"):
            when = datetime.fromtimestamp(content["providerPublishTime"], IST).strftime("%d %b %Y %H:%M")
        elif when:
            when = when[:16].replace("T", " ")
        items.append({"title": title, "publisher": publisher, "link": link, "time": when})
    return {"symbol": sym, "news": items}


# ---------------- AI analysis ----------------

def _rule_based_analysis(snap, news):
    """Deterministic fallback when no Claude API key is configured."""
    m = snap["metrics"]
    sym = snap["symbol"]
    lines = []

    trend = "uptrend" if m["prev_close"] > m["sma20"] > m["sma50"] else \
            "downtrend" if m["prev_close"] < m["sma20"] < m["sma50"] else "sideways / mixed"
    pos52 = (snap["price"] - snap["year_low"]) / max(snap["year_high"] - snap["year_low"], 0.01) * 100
    lines.append(f"**Trend:** {sym} is in a {trend}. Price ₹{snap['price']:,} sits at "
                 f"{pos52:.0f}% of its 52-week range (₹{snap['year_low']:,} – ₹{snap['year_high']:,}). "
                 f"SMA20 ₹{m['sma20']:,} / SMA50 ₹{m['sma50']:,} are the levels to watch.")

    mom = ("strong bullish momentum" if m["rsi"] >= 65 else
           "healthy bullish momentum" if m["rsi"] >= 55 else
           "neutral momentum" if m["rsi"] > 45 else
           "bearish pressure" if m["rsi"] > 30 else "oversold conditions")
    lines.append(f"**Momentum:** RSI(14) at {m['rsi']} shows {mom}. "
                 f"Yesterday's move was {m['day_change_pct']:+}%.")

    vol = ("a strong volume surge — institutions may be active" if m["vol_ratio"] >= 2 else
           "above-average volume — the move has participation" if m["vol_ratio"] >= 1.3 else
           "below-average volume — conviction is low")
    lines.append(f"**Volume:** {m['vol_ratio']}x the 20-day average, {vol}.")

    p = snap["plan"]
    if p["qualifies"]:
        lines.append(f"**Scanner verdict:** {m['side']} setup (score {m['score']}). "
                     f"Plan: entry ₹{p['entry']:,}, stop ₹{p['sl']:,}, target ₹{p['target']:,}, "
                     f"qty {p['qty']}. Reasons: {'; '.join(m['reasons'])}.")
    else:
        lines.append(f"**Scanner verdict:** No intraday edge right now (score {m['score']}, need 5+). "
                     f"Skipping is the disciplined move — no setup ≠ bad stock.")

    if news["news"]:
        lines.append("**News flow:** " + " · ".join(n["title"] for n in news["news"][:3]))

    lines.append("**Risk note:** Average daily range is "
                 f"{m['avg_range_pct']}% — size positions so a {m['avg_range_pct']}% adverse move "
                 "stays within your risk budget. This is rule-based analysis, not advice.")
    return "\n\n".join(lines)


def ai_analyze(symbol):
    """AI take on a stock: Claude if ANTHROPIC_API_KEY is set, else rule-based."""
    snap = analyze_stock(symbol)
    news = stock_news(symbol)

    try:
        import anthropic
        client = anthropic.Anthropic()  # needs ANTHROPIC_API_KEY or ant profile
        m = snap["metrics"]
        context = {
            "symbol": snap["symbol"], "price": snap["price"],
            "change_pct": snap["change_pct"], "52w_high": snap["year_high"],
            "52w_low": snap["year_low"], "rsi14": m["rsi"],
            "volume_vs_20d_avg": m["vol_ratio"], "sma20": m["sma20"],
            "sma50": m["sma50"], "avg_daily_range_pct": m["avg_range_pct"],
            "nifty_trend": snap["nifty_trend"],
            "scanner_verdict": {"side": m["side"], "score": m["score"],
                                "reasons": m["reasons"], "plan": snap["plan"]},
            "recent_news_headlines": [n["title"] for n in news["news"][:6]],
        }
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=16000,
            system=(
                "You are a cautious, honest Indian stock-market analyst. You never "
                "promise returns and always mention risk. Analyze the given NSE stock "
                "snapshot for a retail trader with a small account. Structure: Trend, "
                "Momentum & Volume, Key levels (support/resistance from the data), "
                "News impact (if headlines given), Bull case vs Bear case (2 points "
                "each), and a final Verdict for the next 1-5 sessions with a risk "
                "warning. Be concise — under 300 words. Plain language, no jargon "
                "without explanation. Never invent data not in the snapshot."
            ),
            messages=[{"role": "user", "content": json.dumps(context, indent=1)}],
        )
        if response.stop_reason == "refusal" or not response.content:
            raise RuntimeError("Claude declined")
        text = "".join(b.text for b in response.content if b.type == "text")
        return {"source": "claude", "model": response.model, "symbol": snap["symbol"], "text": text}
    except Exception:
        return {"source": "rules", "symbol": snap["symbol"],
                "text": _rule_based_analysis(snap, news),
                "note": "Rule-based analysis. Set ANTHROPIC_API_KEY and restart to get Claude AI analysis."}


# ---------------- Evaluate ----------------

def run_evaluate():
    if not os.path.exists(LOG_FILE):
        return {"trades": [], "open": [], "summary": None,
                "message": "No signals logged yet. Run a scan first."}
    cfg = load_config()
    rows = list(csv.DictReader(open(LOG_FILE)))
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    changed = False

    for row in rows:
        if row["status"] != "OPEN" or row["date"] > today_str:
            continue
        start = datetime.strptime(row["date"], "%Y-%m-%d")
        df = flatten(yf.download(yahoo_symbol(row["symbol"]), start=start,
                                 end=start + timedelta(days=4), interval="1d",
                                 auto_adjust=True, progress=False))
        df = df[df.index.normalize() == pd.Timestamp(row["date"])]
        if df.empty:
            continue
        hi, lo, close = float(df["High"].iloc[0]), float(df["Low"].iloc[0]), float(df["Close"].iloc[0])
        entry, sl, target = float(row["entry"]), float(row["sl"]), float(row["target"])
        qty = int(row["qty"])

        if row["side"] == "LONG":
            # conservative: if both SL and target were touched, assume SL hit first
            exit_price = sl if lo <= sl else (target if hi >= target else close)
            gross = (exit_price - entry) * qty
        else:
            exit_price = sl if hi >= sl else (target if lo <= target else close)
            gross = (entry - exit_price) * qty

        costs = entry * qty * COST_PCT_ROUNDTRIP / 100
        row["status"] = "CLOSED"
        row["exit_price"] = round(exit_price, 2)
        row["pnl"] = round(gross - costs, 2)
        changed = True

    if changed:
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    closed = [r for r in rows if r["status"] == "CLOSED" and r["pnl"] != ""]
    open_rows = [r for r in rows if r["status"] == "OPEN"]
    summary = None
    if closed:
        pnls = [float(r["pnl"]) for r in closed]
        wins = [p for p in pnls if p > 0]
        total = sum(pnls)
        equity, run = [], 0.0
        for p in pnls:
            run += p
            equity.append(round(run, 2))
        summary = {
            "trades": len(pnls),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(pnls) * 100, 1),
            "net_pnl": round(total, 2),
            "net_pct": round(total / cfg["capital"] * 100, 2),
            "avg_per_trade": round(total / len(pnls), 2),
            "equity_curve": equity,
        }
    return {"trades": closed, "open": open_rows, "summary": summary}


# ---------------- CLI ----------------

def _print_scan(result):
    c = result["config"]
    print(f"\n{'=' * 78}")
    print(f"  NSE 9:15 SCANNER  |  {result['generated_at']}  |  capital Rs.{c['capital']:,}")
    print(f"{'=' * 78}")
    n = result["nifty"]
    print(f"  Nifty: {n['level']:,} ({n['change_pct']:+}%)  trend: {n['trend']}"
          f"  {'(longs favoured)' if n['trend'] == 'UP' else '(shorts favoured)'}")
    if not result["live"]:
        print("  NOTE: market not open today - preview based on last session's data.")
    picks = result["picks"]
    if not picks:
        print("\n  No setups today. NOT trading is also a position - capital preserved.\n")
        return
    print(f"\n  {'SYM':<12}{'SIDE':<7}{'SCORE':<7}{'RSI':<7}{'VOLx':<7}{'GAP%':<7}"
          f"{'ENTRY':<10}{'SL':<10}{'TGT':<10}{'QTY':<6}")
    print(f"  {'-' * 76}")
    for p in picks:
        print(f"  {p['symbol']:<12}{p['side']:<7}{p['score']:<7}{p['rsi']:<7}"
              f"{p['vol_ratio']:<7}{p['gap_pct']:<7}{p['entry']:<10}{p['sl']:<10}"
              f"{p['target']:<10}{p['qty']:<6}")
    print(f"\n  Risk per trade: Rs.{c['capital'] * c['risk_per_trade_pct'] / 100:,.0f}"
          f" ({c['risk_per_trade_pct']}% of capital) | SL {c['stop_loss_pct']}% |"
          f" Target {c['target_pct']}% | est. costs {COST_PCT_ROUNDTRIP}%/trade")
    print("  Rule: SL hit -> exit, no averaging down. 3 losses in a day -> stop for the day.")
    print(f"\n  Signals logged -> {LOG_FILE}")


def _print_report(report):
    s = report["summary"]
    if not s:
        print(report.get("message", "No closed trades to evaluate yet."))
        return
    print(f"\n{'=' * 60}")
    print("  PAPER-TRADE REPORT (the honest mirror)")
    print(f"{'=' * 60}")
    print(f"  Trades: {s['trades']}  |  Wins: {s['wins']}  |  Win rate: {s['win_rate']}%")
    print(f"  Net P&L (after costs): Rs.{s['net_pnl']:,.0f}  ({s['net_pct']:+.2f}% of capital)")
    print(f"  Avg per trade: Rs.{s['avg_per_trade']:,.0f}")
    print(f"\n  {'DATE':<12}{'SYM':<12}{'SIDE':<7}{'ENTRY':<10}{'EXIT':<10}{'P&L':<12}")
    print(f"  {'-' * 58}")
    for r in report["trades"][-15:]:
        print(f"  {r['date']:<12}{r['symbol']:<12}{r['side']:<7}{r['entry']:<10}"
              f"{r['exit_price']:<10}{float(r['pnl']):>+10,.0f}")
    print()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if mode == "scan":
        _print_scan(run_scan())
    elif mode == "evaluate":
        _print_report(run_evaluate())
    else:
        print(__doc__)
