"""GoldSpread configuration — env-loaded, no secrets in source.

Mirrors the FlokiWatch python-dotenv pattern. Loads .env at import,
exposes constants. Fails fast with a clear list of missing required
vars via validate_required().
"""
from __future__ import annotations
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from this file's parent directory (the project root).
_PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(_PROJECT_ROOT / ".env")


# ---------------------------------------------------------------------
# BUILD_TAG — bumped on every code-affecting commit. Used as an
# unambiguous "is the new code running?" marker in startup logs.
# Grep "BUILD=" in logs/goldspread.log to verify.
# ---------------------------------------------------------------------
BUILD_TAG = "phase2.1-duration-filter-and-jpy-sl"


# ---------------------------------------------------------------------
# MT5 credentials
# ---------------------------------------------------------------------
MT5_ACCOUNT = os.environ.get("MT5_ACCOUNT", "").strip()
MT5_PASSWORD = os.environ.get("MT5_PASSWORD", "").strip()
MT5_SERVER = os.environ.get("MT5_SERVER", "").strip()
MT5_TERMINAL_PATH = os.environ.get("MT5_TERMINAL_PATH", "").strip() or None


# ---------------------------------------------------------------------
# GoldSpread settings
# ---------------------------------------------------------------------
TICK_INTERVAL_MS = int(os.environ.get("GOLDSPREAD_TICK_INTERVAL_MS", "200"))
DB_PATH = os.environ.get("GOLDSPREAD_DB_PATH", "data/goldspread.db")
LOG_LEVEL = os.environ.get("GOLDSPREAD_LOG_LEVEL", "INFO").upper()
DAILY_CSV_EXPORT = os.environ.get(
    "GOLDSPREAD_DAILY_CSV_EXPORT", "true"
).lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------
# Symbol universe (Phase 1)
# ---------------------------------------------------------------------
# 6 XAU pairs: 1 anchor (XAUUSD) + 5 derived pairs whose prices are
# triangulable through the corresponding forex rate.
XAU_PAIRS = ("XAUUSD", "XAUEUR", "XAUGBP", "XAUAUD", "XAUJPY", "XAUCHF")
XAU_DERIVED = ("XAUEUR", "XAUGBP", "XAUAUD", "XAUJPY", "XAUCHF")

# 5 forex pairs needed for the triangulation math.
FOREX_PAIRS = ("EURUSD", "GBPUSD", "AUDUSD", "USDJPY", "USDCHF")

ALL_SYMBOLS = XAU_PAIRS + FOREX_PAIRS


# ---------------------------------------------------------------------
# Pip-size convention (price units per pip)
# Display only — load-bearing math is USD-per-XAU, computed in main.py.
# XAU broker convention: 1 pip = 0.01 (one cent of price).
# Forex: 0.0001 standard, 0.01 for JPY pairs.
# ---------------------------------------------------------------------
PIP_SIZE = {
    "XAUUSD": 0.01,
    "XAUEUR": 0.01,
    "XAUGBP": 0.01,
    "XAUAUD": 0.01,
    "XAUJPY": 0.01,
    "XAUCHF": 0.01,
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "AUDUSD": 0.0001,
    "USDCHF": 0.0001,
    "USDJPY": 0.01,
}


# ---------------------------------------------------------------------
# Triangulation map: derived XAU pair -> (forex pair, mode)
#   mode "div": XAU<Q> = XAUUSD / forex   (forex quotes <Q>USD)
#   mode "mul": XAU<Q> = XAUUSD * forex   (forex quotes USD<Q>)
# ---------------------------------------------------------------------
DERIVED_TRIANGLE = {
    "XAUEUR":  ("EURUSD", "div"),   # XAUEUR  = XAUUSD / EURUSD
    "XAUGBP":  ("GBPUSD", "div"),
    "XAUAUD":  ("AUDUSD", "div"),
    "XAUJPY":  ("USDJPY", "mul"),   # XAUJPY = XAUUSD * USDJPY
    "XAUCHF":  ("USDCHF", "mul"),   # XAUCHF = XAUUSD * USDCHF
}


# ---------------------------------------------------------------------
# Phase 2 — Executor settings (DEFAULT OFF)
# ---------------------------------------------------------------------
# Set GOLDSPREAD_EXECUTOR_ENABLED=true to ARM live demo execution.
# Magic 77777 isolates GoldSpread positions from FlokiWatch (234000) —
# the executor NEVER touches positions with any other magic number.
EXECUTOR_ENABLED = os.environ.get(
    "GOLDSPREAD_EXECUTOR_ENABLED", "false"
).lower() in ("1", "true", "yes", "on")
EXECUTOR_MAGIC = int(os.environ.get("GOLDSPREAD_EXECUTOR_MAGIC", "77777"))
EXECUTOR_LOT = float(os.environ.get("GOLDSPREAD_EXECUTOR_LOT", "0.01"))
EXECUTOR_MAX_POSITIONS = int(
    os.environ.get("GOLDSPREAD_EXECUTOR_MAX_POSITIONS", "5"))
EXECUTOR_HOLD_MAX_SECONDS = int(
    os.environ.get("GOLDSPREAD_EXECUTOR_HOLD_MAX_SECONDS", "30"))
EXECUTOR_SL_PIPS = int(os.environ.get("GOLDSPREAD_EXECUTOR_SL_PIPS", "200"))
EXECUTOR_DAILY_LOSS_CAP_USD = float(
    os.environ.get("GOLDSPREAD_EXECUTOR_DAILY_LOSS_CAP_USD", "10.0"))
EXECUTOR_DEVIATION_POINTS = int(
    os.environ.get("GOLDSPREAD_EXECUTOR_DEVIATION_POINTS", "20"))


def validate_required() -> list[str]:
    """Return a list of missing required env-var names. Empty == OK."""
    missing: list[str] = []
    if not MT5_ACCOUNT:
        missing.append("MT5_ACCOUNT")
    if not MT5_PASSWORD:
        missing.append("MT5_PASSWORD")
    if not MT5_SERVER:
        missing.append("MT5_SERVER")
    return missing
