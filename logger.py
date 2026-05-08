"""GoldSpread logger — SQLite persistence + optional daily CSV export.

Single-table wide schema. Idempotent CREATE TABLE. WAL mode + autocommit
so SIGINT cannot corrupt the DB. Per-tick insert is bounded (one
round-trip via prepared statement); fail-soft on individual write
errors (logged at WARN, loop continues).
"""
from __future__ import annotations
import csv
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import config

log = logging.getLogger("goldspread.logger")


def _build_columns() -> List[str]:
    """Construct the canonical column list (order matters for INSERT)."""
    cols: List[str] = ["ts_utc"]

    # XAUUSD anchor (no theoretical/divergence — it's the reference)
    cols += ["xauusd_bid", "xauusd_ask", "xauusd_spread_pips"]

    # 5 derived XAU pairs × 8 columns each
    for pair in config.XAU_DERIVED:
        p = pair.lower()
        cols += [
            f"{p}_bid",
            f"{p}_ask",
            f"{p}_spread_pips",
            f"{p}_theoretical",
            f"{p}_divergence_usd_per_xau",
            f"{p}_xau_spread_usd_per_xau",
            f"{p}_forex_spread_usd_per_xau",
            f"{p}_edge_exists",
        ]

    # 5 forex pairs × 3 columns each
    for fx in config.FOREX_PAIRS:
        f = fx.lower()
        cols += [f"{f}_bid", f"{f}_ask", f"{f}_spread_pips"]

    # Diagnostics
    cols.append("missing_symbols")
    return cols


COLUMNS: List[str] = _build_columns()


def _column_ddl() -> str:
    """Generate the column-list SQL fragment for CREATE TABLE."""
    parts: List[str] = ["id INTEGER PRIMARY KEY AUTOINCREMENT"]
    for col in COLUMNS:
        if col == "ts_utc":
            parts.append(f"{col} TEXT NOT NULL")
        elif col == "missing_symbols":
            parts.append(f"{col} TEXT")
        elif col.endswith("_edge_exists"):
            parts.append(f"{col} INTEGER")
        else:
            parts.append(f"{col} REAL")
    return ",\n    ".join(parts)


def _init_db(db_path: str) -> sqlite3.Connection:
    """Open / create the SQLite DB. Idempotent. Returns a live connection."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)  # autocommit
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"CREATE TABLE IF NOT EXISTS ticks (\n    {_column_ddl()}\n)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ticks_ts_utc ON ticks(ts_utc)")
    log.info("SQLite ready at %s (cols=%d)", db_path, len(COLUMNS))
    return conn


class TickLogger:
    """Persists tick rows to SQLite. One instance per process.

    Usage:
        tl = TickLogger()
        tl.write(row_dict)      # never raises
        ...
        tl.export_daily_csv("2026-05-08")
        tl.close()
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or config.DB_PATH
        self.conn = _init_db(self.db_path)
        col_list = ",".join(COLUMNS)
        placeholders = ",".join("?" for _ in COLUMNS)
        self._insert_sql = (
            f"INSERT INTO ticks ({col_list}) VALUES ({placeholders})"
        )

    def write(self, row: Dict[str, Any]) -> bool:
        """Insert one row. Returns True on success, False on failure
        (logged at WARN). Never raises."""
        try:
            values = [row.get(c) for c in COLUMNS]
            self.conn.execute(self._insert_sql, values)
            return True
        except Exception as e:
            log.warning("DB write failed: %s: %s", type(e).__name__, e)
            return False

    def export_daily_csv(self, day_utc: str) -> Optional[str]:
        """Export all rows where ts_utc starts with day_utc (YYYY-MM-DD).

        Atomic: writes to <out>.tmp then os.replace. Returns final path
        on success, None on failure. Disabled if DAILY_CSV_EXPORT=false.
        """
        if not config.DAILY_CSV_EXPORT:
            return None
        out_path = Path(self.db_path).parent / f"goldspread_{day_utc}.csv"
        tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
        try:
            cur = self.conn.execute(
                f"SELECT {','.join(COLUMNS)} FROM ticks "
                f"WHERE ts_utc LIKE ? ORDER BY ts_utc",
                (f"{day_utc}%",),
            )
            rows = cur.fetchall()
            with open(tmp_path, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(COLUMNS)
                w.writerows(rows)
            os.replace(tmp_path, out_path)
            log.info("CSV export OK: %s (%d rows)", out_path, len(rows))
            return str(out_path)
        except Exception as e:
            log.warning(
                "CSV export failed for %s: %s: %s",
                day_utc, type(e).__name__, e,
            )
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            return None

    def close(self) -> None:
        """Close the SQLite connection. Safe to call multiple times."""
        try:
            self.conn.close()
        except Exception:
            pass
