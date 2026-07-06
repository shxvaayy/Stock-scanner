#!/usr/bin/env python3
"""Local web UI for the NSE scanner. Run: python app.py -> http://localhost:5050"""

import json
import os

from flask import Flask, jsonify, request, send_from_directory

import scanner

app = Flask(__name__, static_folder="static")


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/last")
def api_last():
    """Last scan result (cached), so the dashboard has data on load."""
    if os.path.exists(scanner.LAST_SCAN_FILE):
        with open(scanner.LAST_SCAN_FILE) as f:
            return jsonify(json.load(f))
    return jsonify(None)


@app.post("/api/scan")
def api_scan():
    try:
        return jsonify(scanner.run_scan())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/report")
def api_report():
    try:
        return jsonify(scanner.run_evaluate())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/status")
def api_status():
    try:
        return jsonify(scanner.market_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/config")
def api_config():
    return jsonify(scanner.load_config())


@app.post("/api/config")
def api_config_save():
    try:
        return jsonify(scanner.save_config(request.get_json(force=True) or {}))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.get("/api/stock/<symbol>")
def api_stock(symbol):
    try:
        return jsonify(scanner.analyze_stock(symbol))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/chart/<symbol>")
def api_chart(symbol):
    try:
        rng = request.args.get("range", "3M").upper()
        return jsonify(scanner.chart_data(symbol, rng))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/news/<symbol>")
def api_news(symbol):
    try:
        return jsonify(scanner.stock_news(symbol))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/ai/<symbol>")
def api_ai(symbol):
    try:
        return jsonify(scanner.ai_analyze(symbol))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/movers")
def api_movers():
    try:
        if request.args.get("cached_only"):
            if os.path.exists(scanner.MOVERS_FILE):
                with open(scanner.MOVERS_FILE) as f:
                    return jsonify(json.load(f))
            return jsonify(None)
        return jsonify(scanner.top_movers(force=bool(request.args.get("force"))))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/symbols")
def api_symbols():
    try:
        return jsonify(scanner.full_universe())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("\n  Scanner dashboard -> http://localhost:5050\n")
    app.run(host="127.0.0.1", port=5050, debug=False)
