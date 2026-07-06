#!/usr/bin/env python3
"""AutoTheta web dashboard.

Local:  .venv/bin/python app.py            -> http://localhost:5070
Deploy: gunicorn app:app --bind 0.0.0.0:$PORT
        (set ANGEL_* env vars; TZ=Asia/Kolkata recommended)
"""

import csv
import os
import re
import signal
import subprocess
import sys
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_from_directory

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
PID_FILE = ROOT / "data" / "bot.pid"
CAPITAL = 250000

app = Flask(__name__, static_folder="static")


def ist_now():
    return datetime.now(IST)


def today_str():
    return ist_now().date().isoformat()


def market_state():
    now = ist_now()
    if now.weekday() >= 5:
        return "closed", "Weekend — market closed"
    hm = now.hour * 60 + now.minute
    if hm < 9 * 60:
        return "pre", "Pre-market — opens 9:15 AM IST"
    if hm < 9 * 60 + 15:
        return "pre", "Pre-open session"
    if hm <= 15 * 60 + 30:
        return "open", "Market open — closes 3:30 PM IST"
    return "closed", "Closed 3:30 PM — session done for today"


# ---------------- bot process management ----------------

def bot_pid():
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)  # raises if process is gone
        return pid
    except (OSError, ValueError):
        return None


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


def parse_regime(date_str):
    """Pull the regime-detection block the bot prints at 9:15 startup."""
    out = {}
    for name in ("console.log", "app.log"):
        p = LOGS / date_str / name
        if not p.exists():
            continue
        try:
            txt = p.read_text(errors="ignore")
        except OSError:
            continue
        m = re.findall(r"Market Regime: (\w+)", txt)
        if m:
            out["regime"] = m[-1]
        m = re.findall(r"Nifty: ([\d.]+) \| 200-DMA: ([\d.]+) \| Dist: ([-\d.]+)% "
                       r"\| VIX: ([\d.]+) \| ADX: ([\d.]+)", txt)
        if m:
            nifty, dma, dist, vix, adx = m[-1]
            out.update({"nifty": float(nifty), "dma200": float(dma),
                        "dist_pct": float(dist), "vix": float(vix), "adx": float(adx)})
        m = re.findall(r"IBS: ([\d.]+) \| IBS size mult: ([\d.]+)", txt)
        if m:
            out["ibs"], out["size_mult"] = float(m[-1][0]), float(m[-1][1])
        if out:
            break
    return out


@app.get("/api/status")
def api_status():
    state, detail = market_state()
    pid = bot_pid()
    reg = parse_regime(today_str())
    return jsonify({
        "bot_running": pid is not None,
        "pid": pid,
        "market": state,
        "market_detail": detail,
        "now_ist": ist_now().strftime("%a, %d %b %Y %H:%M IST"),
        "regime": reg.get("regime"),
        "regime_detail": reg,
        "capital": CAPITAL,
        "env_ok": bool(os.environ.get("ANGEL_API_KEY") or (ROOT / ".env").exists()),
        "today": today_str(),
    })


@app.get("/api/summary")
def api_summary():
    """One day, explained: regime, what the bot saw, why it filtered, results."""
    d = request.args.get("date") or today_str()
    thoughts_path = LOGS / d / "thoughts.csv"
    decisions = {}
    filter_reasons = {}
    signals = 0
    if thoughts_path.exists():
        with open(thoughts_path) as f:
            for r in csv.DictReader(f):
                signals += 1
                dec = (r.get("Decision") or "?").strip()
                decisions[dec] = decisions.get(dec, 0) + 1
                if dec == "FILTERED":
                    reason = re.sub(r"[\d.()=]+", "", r.get("Reason") or "").strip()[:60]
                    filter_reasons[reason] = filter_reasons.get(reason, 0) + 1
    rows = read_trades(d)
    pnl, closed, wins = day_pnl(rows)
    entries = sum(1 for r in rows if (r.get("Action") or "").upper() in ("BUY", "ENTRY"))
    top_filters = sorted(filter_reasons.items(), key=lambda x: -x[1])[:4]
    return jsonify({"date": d, "regime": parse_regime(d), "signals": signals,
                    "decisions": decisions, "top_filters": top_filters,
                    "entries": entries, "closed": closed, "wins": wins, "pnl": pnl})


@app.post("/api/bot/start")
def api_bot_start():
    if bot_pid():
        return jsonify({"ok": False, "error": "Bot is already running"})
    state, _ = market_state()
    day_dir = LOGS / today_str()
    day_dir.mkdir(parents=True, exist_ok=True)
    out = open(day_dir / "console.log", "a")
    py = str(ROOT / ".venv" / "bin" / "python3")
    if not os.path.exists(py):
        py = sys.executable
    proc = subprocess.Popen([py, str(ROOT / "paper_live.py")],
                            cwd=str(ROOT), stdout=out, stderr=out,
                            start_new_session=True)
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(proc.pid))
    return jsonify({"ok": True, "pid": proc.pid,
                    "note": "Paper trading only — no real money moves."
                            + ("" if state == "open" else " Market is closed; the bot will idle/exit until the next session.")})


@app.post("/api/bot/stop")
def api_bot_stop():
    pid = bot_pid()
    if not pid:
        return jsonify({"ok": False, "error": "Bot is not running"})
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    PID_FILE.unlink(missing_ok=True)
    return jsonify({"ok": True})


# ---------------- trades / performance ----------------

def read_trades(date_str):
    path = LOGS / date_str / "trades.csv"
    if not path.exists():
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def day_pnl(rows):
    pnl = 0.0
    closed = 0
    wins = 0
    for r in rows:
        val = (r.get("P&L") or "").replace(",", "").strip()
        if val and r.get("Action", "").upper() not in ("BUY", "ENTRY"):
            try:
                v = float(val)
                pnl += v
                closed += 1
                if v > 0:
                    wins += 1
            except ValueError:
                pass
    return round(pnl, 2), closed, wins


@app.get("/api/trades")
def api_trades():
    d = request.args.get("date") or today_str()
    rows = read_trades(d)
    pnl, closed, wins = day_pnl(rows)
    return jsonify({"date": d, "trades": rows, "pnl": pnl,
                    "closed": closed, "wins": wins})


@app.get("/api/days")
def api_days():
    days = []
    if LOGS.exists():
        for p in sorted(LOGS.iterdir()):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name):
                continue
            rows = read_trades(p.name)
            pnl, closed, wins = day_pnl(rows)
            days.append({"date": p.name, "pnl": pnl, "closed": closed,
                         "wins": wins, "actions": len(rows)})
    return jsonify({"days": days, "capital": CAPITAL})


@app.get("/api/thoughts")
def api_thoughts():
    d = request.args.get("date") or today_str()
    n = int(request.args.get("n", 40))
    path = LOGS / d / "thoughts.csv"
    if not path.exists():
        return jsonify({"date": d, "thoughts": []})
    with open(path) as f:
        rows = list(csv.DictReader(f))
    return jsonify({"date": d, "thoughts": rows[-n:]})


@app.get("/api/logs")
def api_logs():
    d = request.args.get("date") or today_str()
    n = int(request.args.get("n", 120))
    out = []
    for name in ("console.log", "app.log"):
        p = LOGS / d / name
        if p.exists():
            try:
                lines = p.read_text(errors="ignore").splitlines()
                out = lines[-n:]
                break
            except OSError:
                pass
    return jsonify({"date": d, "lines": out})


@app.get("/api/report")
def api_report():
    d = request.args.get("date") or today_str()
    p = LOGS / d / "report.txt"
    return jsonify({"date": d,
                    "report": p.read_text(errors="ignore") if p.exists() else None})


if __name__ == "__main__":
    print("\n  AutoTheta dashboard -> http://localhost:5070\n")
    app.run(host="127.0.0.1", port=5070, debug=False)
