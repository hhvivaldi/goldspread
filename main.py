"""GoldSpread main loop — Phase 1 data logger.

Polls MT5 every TICK_INTERVAL_MS, computes triangulated divergences in
USD-per-XAU, and persists to SQLite. NO trading. NO execution. NO live
decisions.

Stop with Ctrl-C. SIGINT/SIGTERM trigger a clean shutdown:
  - export current day's CSV
  - close DB
  - disconnect MT5
"""
from __future__ import annotations
import atexit
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
import feed
from logger import TickLogger
from executor import Executor


log = logging.getLogger("goldspread.main")


# ---------------------------------------------------------------------
# Single-instance lockfile (phase2.7) — prevents the bug from 2026-05-12
# where multiple concurrent main.py processes attached to the same MT5
# terminal under magic=77777 caused retcode=10027 (TOO_MANY_REQUESTS)
# bursts and wasted edges. Lock contents = current PID. On startup, if
# a lockfile exists and its PID is alive, abort. If PID is dead, the
# lock is treated as stale and overwritten.
# ---------------------------------------------------------------------
LOCK_PATH = Path("data") / ".goldspread.lock"


def _pid_alive(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, ValueError):
        return False


def _acquire_lock() -> bool:
    """Return True on success, False if another live instance holds the
    lock. Writes our PID to LOCK_PATH and registers atexit cleanup."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            existing = int(LOCK_PATH.read_text().strip())
        except (ValueError, OSError):
            existing = -1
        if existing > 0 and existing != os.getpid() and _pid_alive(existing):
            log.error(
                "Another GoldSpread instance is already running (PID=%d). "
                "Refusing to start a second instance — this caused the "
                "2026-05-12 retcode=10027 burst. To override, delete %s "
                "after confirming the other process is dead.",
                existing, LOCK_PATH)
            return False
        log.warning(
            "Stale lockfile found (PID=%d not alive). Overwriting.",
            existing)
    LOCK_PATH.write_text(str(os.getpid()))
    atexit.register(_release_lock)
    return True


def _release_lock() -> None:
    try:
        if LOCK_PATH.exists():
            pid_in_file = int(LOCK_PATH.read_text().strip() or "-1")
            if pid_in_file == os.getpid():
                LOCK_PATH.unlink()
    except Exception as e:
        log.warning("lock release failed: %s", e)


# ---------------------------------------------------------------------
# Triangulation math (USD-per-XAU; documented in README §Triangulation)
# ---------------------------------------------------------------------
def _mid(bid: float, ask: float) -> float:
    return (bid + ask) / 2.0


def _compute_derived(
    pair: str,
    xau_real: Optional[Tuple[float, float]],
    xauusd: Optional[Tuple[float, float]],
    forex: Optional[Tuple[float, float]],
) -> Dict[str, Optional[float]]:
    """Compute the 5 derived metrics for one XAU<quote> pair.

    Returns a dict with keys:
      theoretical
      divergence_usd_per_xau
      xau_spread_usd_per_xau
      forex_spread_usd_per_xau
      edge_exists           (0 / 1 / None)

    All values are None if any required input is missing.
    """
    out: Dict[str, Optional[float]] = {
        "theoretical": None,
        "divergence_usd_per_xau": None,
        "xau_spread_usd_per_xau": None,
        "forex_spread_usd_per_xau": None,
        "edge_exists": None,
    }
    if xauusd is None or forex is None or xau_real is None:
        return out

    xauusd_mid = _mid(*xauusd)
    forex_mid = _mid(*forex)
    forex_spread = forex[1] - forex[0]
    if forex_mid <= 0:
        return out

    forex_pair, mode = config.DERIVED_TRIANGLE[pair]
    xau_real_mid = _mid(*xau_real)
    xau_spread_quote = xau_real[1] - xau_real[0]

    if mode == "div":
        # XAU<Q> = XAUUSD / forex; forex quotes <Q>USD ⇒ USD per Q = forex_mid
        theo = xauusd_mid / forex_mid
        usd_per_quote = forex_mid
        # forex_spread propagation: position = xau_real_mid <Q>; hedge cost
        # in USD = xau_real_mid × spread_<Q>USD (USD per Q).
        forex_spread_usd_per_xau = xau_real_mid * forex_spread
    else:  # mode == "mul"
        # XAU<Q> = XAUUSD * forex; forex quotes USD<Q> ⇒ USD per Q = 1/forex_mid
        theo = xauusd_mid * forex_mid
        usd_per_quote = 1.0 / forex_mid
        # forex_spread propagation (derivative of XAU<Q>/forex w.r.t. forex):
        # USD per XAU = (XAUUSD * spread_USD<Q>) / forex_mid
        forex_spread_usd_per_xau = xauusd_mid * forex_spread / forex_mid

    out["theoretical"] = theo
    out["xau_spread_usd_per_xau"] = xau_spread_quote * usd_per_quote
    out["forex_spread_usd_per_xau"] = forex_spread_usd_per_xau

    divergence_quote = xau_real_mid - theo
    out["divergence_usd_per_xau"] = divergence_quote * usd_per_quote

    total_spread = (
        out["xau_spread_usd_per_xau"] + out["forex_spread_usd_per_xau"]
    )
    out["edge_exists"] = (
        1 if abs(out["divergence_usd_per_xau"]) > total_spread else 0
    )
    return out


def _spread_pips(
    symbol: str,
    bid_ask: Optional[Tuple[float, float]],
) -> Optional[float]:
    if bid_ask is None:
        return None
    pip = config.PIP_SIZE.get(symbol, 0.0001)
    return (bid_ask[1] - bid_ask[0]) / pip


def _build_row(
    ts_utc: str,
    data: Dict[str, Optional[Tuple[float, float]]],
    missing: List[str],
) -> Tuple[Dict[str, Any], int]:
    """Construct a row dict for TickLogger.write(). Returns (row, edges_count)."""
    row: Dict[str, Any] = {"ts_utc": ts_utc}

    # XAUUSD anchor
    xauusd = data.get("XAUUSD")
    if xauusd is not None:
        row["xauusd_bid"] = xauusd[0]
        row["xauusd_ask"] = xauusd[1]
        row["xauusd_spread_pips"] = _spread_pips("XAUUSD", xauusd)
    else:
        row["xauusd_bid"] = None
        row["xauusd_ask"] = None
        row["xauusd_spread_pips"] = None

    edges_this_tick = 0
    for pair in config.XAU_DERIVED:
        p = pair.lower()
        xau_real = data.get(pair)
        forex_pair, _ = config.DERIVED_TRIANGLE[pair]
        forex = data.get(forex_pair)

        if xau_real is not None:
            row[f"{p}_bid"] = xau_real[0]
            row[f"{p}_ask"] = xau_real[1]
            row[f"{p}_spread_pips"] = _spread_pips(pair, xau_real)
        else:
            row[f"{p}_bid"] = None
            row[f"{p}_ask"] = None
            row[f"{p}_spread_pips"] = None

        derived = _compute_derived(pair, xau_real, xauusd, forex)
        row[f"{p}_theoretical"] = derived["theoretical"]
        row[f"{p}_divergence_usd_per_xau"] = derived["divergence_usd_per_xau"]
        row[f"{p}_xau_spread_usd_per_xau"] = derived["xau_spread_usd_per_xau"]
        row[f"{p}_forex_spread_usd_per_xau"] = derived["forex_spread_usd_per_xau"]
        row[f"{p}_edge_exists"] = derived["edge_exists"]
        if derived["edge_exists"] == 1:
            edges_this_tick += 1

    for fx in config.FOREX_PAIRS:
        f = fx.lower()
        bid_ask = data.get(fx)
        if bid_ask is not None:
            row[f"{f}_bid"] = bid_ask[0]
            row[f"{f}_ask"] = bid_ask[1]
            row[f"{f}_spread_pips"] = _spread_pips(fx, bid_ask)
        else:
            row[f"{f}_bid"] = None
            row[f"{f}_ask"] = None
            row[f"{f}_spread_pips"] = None

    row["missing_symbols"] = ",".join(missing) if missing else None
    return row, edges_this_tick


# ---------------------------------------------------------------------
# Heartbeat — one summary log line per minute (avoids 1Hz spam)
# ---------------------------------------------------------------------
class _MinuteStats:
    def __init__(self) -> None:
        self.n_ticks = 0
        self.n_writes_ok = 0
        self.n_missing_total = 0
        self.n_edges_total = 0
        self.last_log_minute: Optional[str] = None

    def record(self, ok: bool, missing: List[str], edges: int) -> None:
        self.n_ticks += 1
        if ok:
            self.n_writes_ok += 1
        self.n_missing_total += len(missing)
        self.n_edges_total += edges

    def maybe_log(self, ts_utc: str) -> None:
        minute = ts_utc[:16]  # "YYYY-MM-DDTHH:MM"
        if self.last_log_minute is None:
            self.last_log_minute = minute
            return
        if minute != self.last_log_minute:
            log.info(
                "heartbeat min=%s ticks=%d writes_ok=%d edges_seen=%d "
                "avg_missing=%.2f",
                self.last_log_minute,
                self.n_ticks,
                self.n_writes_ok,
                self.n_edges_total,
                self.n_missing_total / max(1, self.n_ticks),
            )
            self.n_ticks = 0
            self.n_writes_ok = 0
            self.n_missing_total = 0
            self.n_edges_total = 0
            self.last_log_minute = minute


# ---------------------------------------------------------------------
# Loop control
# ---------------------------------------------------------------------
_running = True


def _handle_signal(signum, frame):
    global _running
    log.info("Signal %s received — shutting down", signum)
    _running = False


def main() -> int:
    Path("logs").mkdir(parents=True, exist_ok=True)
    log_file = "logs/goldspread.log"
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    log.warning(
        "===== GoldSpread STARTUP | BUILD=%s | tick_interval_ms=%d | "
        "db=%s | executor_enabled=%s | PID=%d =====",
        config.BUILD_TAG, config.TICK_INTERVAL_MS, config.DB_PATH,
        config.EXECUTOR_ENABLED, os.getpid(),
    )

    if not _acquire_lock():
        return 3

    missing_env = config.validate_required()
    if missing_env:
        log.error("Missing required env vars: %s — aborting", missing_env)
        return 1

    try:
        feed.connect()
    except Exception as e:
        log.error("MT5 connect failed: %s", e)
        return 2

    tl = TickLogger()
    stats = _MinuteStats()
    executor = Executor()  # no-op if EXECUTOR_ENABLED=false

    signal.signal(signal.SIGINT, _handle_signal)
    # SIGTERM is a no-op on Windows but registering is harmless.
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except Exception:
        pass

    interval_s = config.TICK_INTERVAL_MS / 1000.0
    last_csv_export_day: Optional[str] = None

    try:
        while _running:
            t0 = time.monotonic()
            now = datetime.now(timezone.utc)
            ts_utc = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            day_utc = ts_utc[:10]

            data, missing = feed.read_all()
            row, edges = _build_row(ts_utc, data, missing)
            ok = tl.write(row)
            stats.record(ok, missing, edges)
            stats.maybe_log(ts_utc)

            # Phase 2: synchronous executor hook. No-op if disabled.
            executor.on_tick(row, ts_utc)

            # Daily CSV rollover (export the day that just ended).
            if last_csv_export_day is None:
                last_csv_export_day = day_utc
            elif day_utc != last_csv_export_day:
                tl.export_daily_csv(last_csv_export_day)
                last_csv_export_day = day_utc

            elapsed = time.monotonic() - t0
            sleep_for = max(0.0, interval_s - elapsed)
            time.sleep(sleep_for)
    finally:
        log.info("Shutdown sequence")
        executor.shutdown()  # closes magic=77777 positions + DB conn
        if last_csv_export_day:
            tl.export_daily_csv(last_csv_export_day)
        tl.close()
        feed.shutdown()
        log.info("GoldSpread stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
