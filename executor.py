"""GoldSpread Phase 2 — demo executor.

OPERATIONAL CONTRACT (CRITICAL):
  - Operates ONLY on positions tagged magic=77777. NEVER touches
    positions with any other magic number. FlokiWatch uses 234000;
    the two systems coexist on the same MT5 terminal by magic
    isolation alone.
  - Default DISABLED. Requires GOLDSPREAD_EXECUTOR_ENABLED=true at
    startup. Cannot be enabled mid-process.
  - Demo only. The MT5 session attached by feed.connect() is the
    Capital Point demo (real execution, fake $); this module sends
    real order_send calls.
  - Fail-closed: any guard failure or exception → skip the trade,
    never proceed under uncertainty.

GUARDS (executed in order; first failure aborts):
  1. account_info() reachable (terminal connected, logged in)
  2. no existing position on the same symbol (magic=77777)
  3. our open-position count < EXECUTOR_MAX_POSITIONS
  4. daily realized P&L > -EXECUTOR_DAILY_LOSS_CAP_USD
  5. divergence sign valid (+ → SELL, - → BUY; 0 → skip)
  6. symbol_info_tick() returns positive bid/ask
  7. symbol_info().trade_mode == FULL (market open & tradable)

CLOSE CONDITIONS (per position, evaluated each tick):
  a. edge_exists for that pair flipped to 0 in this tick's row
  b. position has been open for > EXECUTOR_HOLD_MAX_SECONDS

AUDIT:
  Every OPEN and CLOSE event writes a row to the `trades` table in
  the shared GoldSpread DB (autocommit + WAL). Index on ts_utc and
  ticket. Per-event fields include divergence and spread at entry
  for post-hoc analysis.

THREAD MODEL:
  Single-threaded. Called synchronously from main.py's tick loop.
  Order placement is bounded by deviation=20 points but a slow
  broker round-trip can extend the tick interval beyond 200 ms; the
  next tick is processed once on_tick() returns.
"""
from __future__ import annotations
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import MetaTrader5 as mt5  # type: ignore[import-not-found]

import config

log = logging.getLogger("goldspread.executor")


class Executor:
    """Demo executor for GoldSpread arbitrage signals.

    Public surface:
      on_tick(row, ts_utc)  Called from main.py every loop iteration.
      shutdown()            Closes any open magic=77777 positions and
                            disposes the audit DB connection.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.enabled: bool = config.EXECUTOR_ENABLED
        self.magic: int = config.EXECUTOR_MAGIC
        self.lot: float = config.EXECUTOR_LOT
        self.max_positions: int = config.EXECUTOR_MAX_POSITIONS
        self.hold_max_seconds: int = config.EXECUTOR_HOLD_MAX_SECONDS
        self.sl_pips: int = config.EXECUTOR_SL_PIPS
        self.daily_loss_cap: float = config.EXECUTOR_DAILY_LOSS_CAP_USD
        self.deviation: int = config.EXECUTOR_DEVIATION_POINTS
        self.min_streak: int = config.EXECUTOR_MIN_STREAK

        # In-memory state
        self._open_times: Dict[int, float] = {}  # ticket -> monotonic open time
        self._cooldown: Dict[str, float] = {}  # symbol -> earliest next try (monotonic)
        self._edge_streak: Dict[str, int] = {}  # Bug 1: consecutive edge=1 ticks per pair
        self._daily_realized: float = 0.0
        self._daily_realized_date: Optional[str] = None

        self.db_path: str = db_path or config.DB_PATH
        self.conn: Optional[sqlite3.Connection] = None

        if self.enabled:
            self._init_audit_table()
            self._reseed_open_times()
            self._refresh_daily_date(
                datetime.now(timezone.utc).strftime("%Y-%m-%d"))
            log.warning(
                "GoldSpread EXECUTOR ENABLED | BUILD=%s | magic=%d lot=%s "
                "max_positions=%d sl_pips=%d hold_max_s=%d "
                "daily_loss_cap=$%.2f deviation_pts=%d min_streak=%d",
                config.BUILD_TAG, self.magic, self.lot, self.max_positions,
                self.sl_pips, self.hold_max_seconds, self.daily_loss_cap,
                self.deviation, self.min_streak,
            )
        else:
            log.info(
                "GoldSpread EXECUTOR DISABLED "
                "(set GOLDSPREAD_EXECUTOR_ENABLED=true to arm)"
            )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def on_tick(self, row: Dict[str, Any], ts_utc: str) -> None:
        """Called every tick. No-op if disabled. Never raises."""
        if not self.enabled:
            return
        try:
            # Roll over realized P&L tracker at UTC day flip
            today = ts_utc[:10]
            if today != self._daily_realized_date:
                self._refresh_daily_date(today)

            # Update consecutive-edge streak per pair (Bug 1 — duration filter)
            self._update_edge_streak(row)

            # 1) Manage existing positions FIRST (close conditions)
            self._manage_open_positions(row, ts_utc)

            # 2) Then consider opening new ones
            self._maybe_open_new(row, ts_utc)
        except Exception as e:
            log.exception(
                "on_tick uncaught: %s: %s", type(e).__name__, e)

    def shutdown(self) -> None:
        """Close all magic=77777 positions; close audit DB connection."""
        if not self.enabled:
            return
        try:
            now_iso = datetime.now(timezone.utc).isoformat(
                timespec="milliseconds").replace("+00:00", "Z")
            try:
                positions = mt5.positions_get() or ()
            except Exception:
                positions = ()
            for p in positions:
                if p.magic == self.magic:
                    self._close_position(p, "shutdown", now_iso)
            log.warning(
                "executor shutdown complete | daily_realized=$%.2f",
                self._daily_realized)
        except Exception as e:
            log.warning("executor shutdown error: %s: %s",
                        type(e).__name__, e)
        finally:
            try:
                if self.conn is not None:
                    self.conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Initialization helpers
    # ------------------------------------------------------------------
    def _init_audit_table(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL,
                event TEXT NOT NULL,
                symbol TEXT,
                direction TEXT,
                ticket INTEGER,
                price REAL,
                volume REAL,
                sl REAL,
                profit REAL,
                close_reason TEXT,
                divergence_usd_per_xau REAL,
                spread_pips REAL,
                magic INTEGER
            )
        """)
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_ts_utc ON trades(ts_utc)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_ticket ON trades(ticket)")

    def _reseed_open_times(self) -> None:
        """At startup, detect any orphan magic=77777 positions from a
        prior run and seed their open_time = now. They will be managed
        as if just opened (subject to 30s timeout from this moment)."""
        try:
            positions = mt5.positions_get() or ()
        except Exception as e:
            log.debug("positions_get on startup failed: %s", e)
            return
        now = time.monotonic()
        orphans = [p for p in positions if p.magic == self.magic]
        for p in orphans:
            self._open_times[p.ticket] = now
            log.warning(
                "ORPHAN position from prior run | ticket=%d sym=%s "
                "vol=%.2f — managing as if just opened",
                p.ticket, p.symbol, float(p.volume),
            )

    def _refresh_daily_date(self, today: str) -> None:
        """Reset realized accumulator at UTC day flip. In-memory only —
        a process restart loses the accumulator. The 200-pip SL on each
        position bounds the worst-case loss-per-position regardless."""
        self._daily_realized_date = today
        self._daily_realized = 0.0

    def _update_edge_streak(self, row: Dict[str, Any]) -> None:
        """Bug 1 fix — track consecutive edge=1 ticks per derived pair.

        Increments the per-pair counter when edge_exists==1; resets to 0
        when the value is anything else (0 or None). Used by
        _maybe_open_new to require >= 2 consecutive ticks (>= 400 ms)
        before opening, filtering out single-tick (200 ms) flashes.
        """
        for pair in config.XAU_DERIVED:
            edge = row.get(f"{pair.lower()}_edge_exists")
            if edge == 1:
                self._edge_streak[pair] = self._edge_streak.get(pair, 0) + 1
            else:
                self._edge_streak[pair] = 0

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------
    def _manage_open_positions(self, row: Dict[str, Any], ts_utc: str) -> None:
        try:
            positions = mt5.positions_get() or ()
        except Exception as e:
            log.warning("positions_get failed in manage: %s", e)
            return
        now = time.monotonic()
        for p in positions:
            if p.magic != self.magic:
                continue  # NEVER touch other magic numbers
            edge_col = f"{p.symbol.lower()}_edge_exists"
            edge_value = row.get(edge_col)
            opened_at = self._open_times.get(p.ticket, now)
            held_seconds = now - opened_at

            close_reason: Optional[str] = None
            if edge_value == 0:
                close_reason = "edge_lost"
            elif held_seconds > self.hold_max_seconds:
                close_reason = "timeout_30s"

            if close_reason is not None:
                self._close_position(p, close_reason, ts_utc)

    def _maybe_open_new(self, row: Dict[str, Any], ts_utc: str) -> None:
        # GUARD 1: daily loss cap
        if self._daily_realized <= -self.daily_loss_cap:
            return  # no log spam — silently halt opens until next UTC day

        # GUARD 2: terminal connected & logged in
        if not self._is_market_ok():
            return

        # GUARD 3: max positions
        try:
            positions = mt5.positions_get() or ()
        except Exception as e:
            log.warning("positions_get failed in maybe_open: %s", e)
            return
        ours = [p for p in positions if p.magic == self.magic]
        if len(ours) >= self.max_positions:
            return
        open_pairs = {p.symbol for p in ours}

        for pair in config.XAU_DERIVED:
            pair_lower = pair.lower()
            edge = row.get(f"{pair_lower}_edge_exists")
            if edge != 1:
                continue

            # GUARD 4: no duplicate on same pair
            if pair in open_pairs:
                continue

            # GUARD 5: cooldown (anti-double-fire on rapid ticks)
            if time.monotonic() < self._cooldown.get(pair, 0.0):
                continue

            # GUARD 5b (Bug 1, configurable phase2.4): require >= self.min_streak
            # consecutive edge=1 ticks. At 200 ms tick interval:
            #   2 ticks = 400 ms (phase2.1 original)
            #   5 ticks = 1.0 s (phase2.4 default - covers typical broker
            #                    round-trip 73-703 ms observed)
            # Override via GOLDSPREAD_EXECUTOR_MIN_STREAK env var.
            if self._edge_streak.get(pair, 0) < self.min_streak:
                continue

            # GUARD 6: valid divergence sign
            div = row.get(f"{pair_lower}_divergence_usd_per_xau")
            if div is None or div == 0:
                continue
            direction = "BUY" if div < 0 else "SELL"

            spread_pips = row.get(f"{pair_lower}_spread_pips")
            self._open_position(pair, direction, div, spread_pips, ts_utc)

            # Re-check max_positions after each successful open
            if len(self._open_times) >= self.max_positions:
                break

    def _is_market_ok(self) -> bool:
        try:
            return mt5.account_info() is not None
        except Exception:
            return False

    def _cooldown_for_retcode(self, rcode: Optional[int]) -> float:
        """Map MT5 retcode to appropriate cooldown duration in seconds.

        Bug 3 fix: retcode=10018 (TRADE_RETCODE_MARKET_CLOSED) means the
        broker is closed for trading on this symbol. Retrying every tick
        (200 ms) spams the broker for the full duration of market
        closure. Cool down for 5 min so we only attempt 12 times/hour
        instead of 18,000.
        """
        if rcode is None:
            return 1.0
        # 10018 = TRADE_RETCODE_MARKET_CLOSED
        if rcode == 10018:
            return 300.0
        # 10017 = TRADE_RETCODE_TRADE_DISABLED (broker-side disable)
        if rcode == 10017:
            return 60.0
        # 10016 = TRADE_RETCODE_INVALID_STOPS — longer cooldown so the
        # next attempt picks up fresh stops_level data from the broker
        if rcode == 10016:
            return 5.0
        # Default: short cooldown, transient issue
        return 1.0

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------
    def _open_position(
        self,
        symbol: str,
        direction: str,
        divergence: float,
        spread_pips: Optional[float],
        ts_utc: str,
    ) -> None:
        # GUARD 7: tick freshness + tradable
        try:
            tick = mt5.symbol_info_tick(symbol)
            sym_info = mt5.symbol_info(symbol)
        except Exception as e:
            log.warning("open(%s): symbol info exception: %s", symbol, e)
            self._cooldown[symbol] = time.monotonic() + 1.0
            return
        if tick is None or tick.bid <= 0 or tick.ask <= 0:
            self._cooldown[symbol] = time.monotonic() + 1.0
            return
        if sym_info is None or sym_info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
            log.warning(
                "open(%s): trade_mode != FULL (mode=%s) — skipping",
                symbol, getattr(sym_info, "trade_mode", "?"))
            self._cooldown[symbol] = time.monotonic() + 5.0
            return

        # Bug 2/4 fix — respect broker's minimum SL distance. Some brokers
        # use trade_stops_level, others use trade_freeze_level, and a few
        # populate both. Take the max of both as the floor and apply a 25%
        # safety buffer (was 10%; bumped after XAUAUD still failed with
        # retcode=10016). Empirical:
        #   - XAUJPY rejected with 10016 because stops_level*point (~2 JPY)
        #     was below broker minimum at ~740,000 JPY price
        #   - XAUAUD rejected even with the existing 10% buffer
        point = float(getattr(sym_info, "point", 0) or 0.01)
        stops_level_pts = int(getattr(sym_info, "trade_stops_level", 0) or 0)
        freeze_level_pts = int(getattr(sym_info, "trade_freeze_level", 0) or 0)
        broker_floor_pts = max(stops_level_pts, freeze_level_pts)
        digits = int(getattr(sym_info, "digits", 2) or 2)
        broker_min_distance = broker_floor_pts * point

        sl_distance = self.sl_pips * 0.01  # configured XAU-pip distance
        if broker_min_distance > sl_distance:
            sl_distance = broker_min_distance * 1.25  # +25% safety buffer
            log.info(
                "open(%s): SL distance bumped to %.5f "
                "(stops_level=%d pts, freeze_level=%d pts, floor=%d pts "
                "x point=%.5f = %.5f, +25%% buffer)",
                symbol, sl_distance, stops_level_pts, freeze_level_pts,
                broker_floor_pts, point, broker_min_distance,
            )
        else:
            log.debug(
                "open(%s): SL distance %.5f >= broker min %.5f "
                "(stops_level=%d, freeze_level=%d) - no bump needed",
                symbol, sl_distance, broker_min_distance,
                stops_level_pts, freeze_level_pts,
            )

        if direction == "BUY":
            price = tick.ask
            sl = round(price - sl_distance, digits)
            order_type = mt5.ORDER_TYPE_BUY
        else:  # SELL
            price = tick.bid
            sl = round(price + sl_distance, digits)
            order_type = mt5.ORDER_TYPE_SELL

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": self.lot,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": 0.0,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": "GoldSpread",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        try:
            result = mt5.order_send(request)
        except Exception as e:
            log.exception("order_send(%s) raised: %s: %s",
                          symbol, type(e).__name__, e)
            self._cooldown[symbol] = time.monotonic() + 1.0
            return

        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            rcode = getattr(result, "retcode", None)
            err = mt5.last_error()
            cooldown_s = self._cooldown_for_retcode(rcode)
            log.warning(
                "open(%s, %s) FAILED retcode=%s last_error=%s "
                "price=%.5f sl=%.5f cooldown=%.0fs",
                symbol, direction, rcode, err, price, sl, cooldown_s)
            self._cooldown[symbol] = time.monotonic() + cooldown_s
            return

        ticket = int(result.order)
        fill_price = float(result.price)
        self._open_times[ticket] = time.monotonic()
        self._cooldown[symbol] = time.monotonic() + 0.5  # 500ms anti-double
        log.info(
            "OPEN %s %s ticket=%d price=%.5f sl=%.5f div=%.4f spread_pips=%s",
            symbol, direction, ticket, fill_price, sl, divergence,
            f"{spread_pips:.1f}" if spread_pips is not None else "None",
        )
        self._audit(
            ts_utc=ts_utc, event="OPEN", symbol=symbol, direction=direction,
            ticket=ticket, price=fill_price, volume=self.lot, sl=sl,
            profit=None, close_reason=None,
            divergence_usd_per_xau=divergence, spread_pips=spread_pips,
            magic=self.magic,
        )

    def _close_position(self, p: Any, reason: str, ts_utc: str) -> None:
        try:
            tick = mt5.symbol_info_tick(p.symbol)
        except Exception as e:
            log.warning("close(%s): tick exception: %s", p.symbol, e)
            return
        if tick is None:
            log.warning("close(%s): no tick — deferring", p.symbol)
            return

        if p.type == mt5.POSITION_TYPE_BUY:
            close_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            close_type = mt5.ORDER_TYPE_BUY
            price = tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": int(p.ticket),
            "symbol": p.symbol,
            "volume": float(p.volume),
            "type": close_type,
            "price": price,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": f"GS_close_{reason[:14]}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        try:
            result = mt5.order_send(request)
        except Exception as e:
            log.exception("close order_send(%s) raised: %s: %s",
                          p.symbol, type(e).__name__, e)
            return

        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            rcode = getattr(result, "retcode", None)
            err = mt5.last_error()
            log.warning(
                "close(%s, ticket=%d, reason=%s) FAILED retcode=%s err=%s",
                p.symbol, p.ticket, reason, rcode, err)
            return

        # Fix 1 (Bug 5, phase2.4) — prefer the real DEAL profit over MTM
        # snapshot. p.profit is the position's mark-to-market valued at the
        # bid (for SELL) or ask (for BUY) when positions_get() was last
        # called; the actual close fill price diverges from this when the
        # bid-ask span is wide. Empirical: on 2026-05-11, ticket 1639024535
        # showed DB=-0.31 but real deal profit=+1.39 (a $1.70 swing for one
        # XAUGBP SELL with 24-pip spread). Query history_deals_get with the
        # deal_id returned by order_send and use its realized profit; fall
        # back to MTM if the lookup fails so we never lose a close audit.
        realized_mtm = (
            float(p.profit)
            + float(getattr(p, "commission", 0.0) or 0.0)
            + float(getattr(p, "swap", 0.0) or 0.0)
        )
        realized = realized_mtm
        profit_source = "mtm_snapshot"
        deal_id = getattr(result, "deal", None) or 0
        try:
            if deal_id:
                deal_rows = mt5.history_deals_get(ticket=int(deal_id))
                if deal_rows:
                    od = deal_rows[0]
                    realized = (
                        float(od.profit)
                        + float(getattr(od, "commission", 0.0) or 0.0)
                        + float(getattr(od, "swap", 0.0) or 0.0)
                    )
                    profit_source = "history_deal"
        except Exception as e:
            log.debug("history_deals_get(ticket=%s) raised %s: %s",
                      deal_id, type(e).__name__, e)

        self._daily_realized += realized
        self._open_times.pop(int(p.ticket), None)
        direction = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
        log.info(
            "CLOSE %s %s ticket=%d reason=%s price=%.5f profit=%+.2f "
            "(src=%s mtm=%+.2f) daily_realized=%+.2f",
            p.symbol, direction, p.ticket, reason, float(result.price),
            realized, profit_source, realized_mtm, self._daily_realized,
        )
        self._audit(
            ts_utc=ts_utc, event="CLOSE", symbol=p.symbol,
            direction=direction, ticket=int(p.ticket),
            price=float(result.price), volume=float(p.volume), sl=None,
            profit=realized, close_reason=reason,
            divergence_usd_per_xau=None, spread_pips=None,
            magic=self.magic,
        )

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------
    def _audit(self, **fields: Any) -> None:
        if self.conn is None:
            return
        try:
            cols = list(fields.keys())
            vals = [fields[c] for c in cols]
            placeholders = ",".join("?" for _ in cols)
            self.conn.execute(
                f"INSERT INTO trades ({','.join(cols)}) "
                f"VALUES ({placeholders})", vals)
        except Exception as e:
            log.warning("audit insert failed: %s: %s", type(e).__name__, e)
