"""GoldSpread feed — MT5 connection + tick reading.

Single-threaded by design (main.py is the only consumer), so this
module imports MetaTrader5 directly without the FlokiWatch mt5_safe
proxy — there is no concurrent caller to race against.

Public surface:
  connect()           initialize MT5 + login + select all symbols. Raises on fatal init failure.
  read_tick(symbol)   return (bid, ask) or None on failure. NEVER raises.
  read_all()          single-shot read of every symbol. Returns (data_dict, missing_list).
  shutdown()          clean MT5 disconnect. Safe to call multiple times.
"""
from __future__ import annotations
import logging
from typing import Dict, List, Optional, Tuple

import MetaTrader5 as mt5  # type: ignore[import-not-found]

import config

log = logging.getLogger("goldspread.feed")


def connect() -> None:
    """Attach to the running MT5 terminal in read-only mode.

    GoldSpread shares the MT5 terminal with FlokiWatch. We must NOT pass
    login/password/server — doing so forces a re-login on the running
    session and disconnects FlokiWatch (empirical: confirmed 2026-05-08).

    The MetaTrader5 Python library, when initialize() is called with no
    credentials, attaches to whatever terminal session is currently
    logged in. The only optional argument is `path` — used solely to
    disambiguate when multiple terminals are installed on the machine.

    Read-only contract: this module calls only symbol_info_tick(),
    symbol_select(), account_info() (informational), and shutdown().
    No order placement, no plan submission, no state mutation on the
    broker side.

    Fatal if no terminal is available to attach to (account_info()
    returns None — meaning Floki hasn't logged in yet, or terminal is
    closed). If symbol_select fails for any individual symbol, that
    symbol is logged once at WARN level and will return None for every
    subsequent read — the run continues with the rest.
    """
    init_kwargs: Dict[str, object] = {}
    # Path-only attach. login/password/server intentionally NOT passed —
    # see docstring above.
    if config.MT5_TERMINAL_PATH:
        init_kwargs["path"] = config.MT5_TERMINAL_PATH

    if not mt5.initialize(**init_kwargs):
        err = mt5.last_error()
        raise RuntimeError(
            f"MT5 attach failed: {err}. Is the terminal running and "
            f"logged in (e.g. via FlokiWatch)?"
        )

    info = mt5.account_info()
    if info is None:
        mt5.shutdown()
        raise RuntimeError(
            "MT5 attached but account_info() returned None — terminal "
            "appears not logged in. Start FlokiWatch (or log into the "
            "terminal manually) before running GoldSpread."
        )
    log.info(
        "MT5 attached (read-only) | account=%s server=%s balance=%s leverage=%s",
        info.login, info.server, info.balance, info.leverage,
    )

    available: List[str] = []
    unavailable: List[str] = []
    for symbol in config.ALL_SYMBOLS:
        if mt5.symbol_select(symbol, True):
            available.append(symbol)
        else:
            unavailable.append(symbol)
    log.info("Symbols available (%d): %s", len(available), available)
    if unavailable:
        log.warning(
            "Symbols UNAVAILABLE on this broker (%d): %s — those columns "
            "will be NULL for the entire run",
            len(unavailable), unavailable,
        )


def read_tick(symbol: str) -> Optional[Tuple[float, float]]:
    """Return (bid, ask) for `symbol` or None on any failure.

    Never raises. Symbol-not-found, MT5 disconnected, zero/negative
    prices all collapse to None.
    """
    try:
        tick = mt5.symbol_info_tick(symbol)
    except Exception as e:
        log.debug("symbol_info_tick(%s) raised %s: %s",
                  symbol, type(e).__name__, e)
        return None
    if tick is None:
        return None
    bid = float(getattr(tick, "bid", 0) or 0)
    ask = float(getattr(tick, "ask", 0) or 0)
    if bid <= 0 or ask <= 0:
        return None
    return bid, ask


def read_all() -> Tuple[Dict[str, Optional[Tuple[float, float]]], List[str]]:
    """Read every configured symbol once.

    Returns:
      data    : dict[symbol -> (bid, ask) or None]
      missing : list of symbols that returned None this read
    """
    data: Dict[str, Optional[Tuple[float, float]]] = {}
    missing: List[str] = []
    for symbol in config.ALL_SYMBOLS:
        result = read_tick(symbol)
        data[symbol] = result
        if result is None:
            missing.append(symbol)
    return data, missing


def shutdown() -> None:
    """Clean MT5 disconnect. Safe to call multiple times / when not connected."""
    try:
        mt5.shutdown()
    except Exception as e:
        log.debug("mt5.shutdown raised %s: %s", type(e).__name__, e)
