"""Unified backtest harness for all 6 AutoTheta strategies.

Cache-only: every loader reads data/backtest_cache/ written by
scripts/fetch_history.py. No API calls happen during a backtest run.
"""
