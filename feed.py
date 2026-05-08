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
    """Initialize MT5 and select every configured symbol.

    Fatal if mt5.initialize() or login fails. If symbol_select fails for
    any individual symbol, that symbol is logged once at WARN level and
    will return None for every subsequent read — the run continues with
    the rest of the symbols.
    """
    init_kwargs: Dict[str, object] = {}
    if config.MT5_TERMINAL_PATH:
        init_kwargs["path"] = config.MT5_TERMINAL_PATH
    if config.MT5_ACCOUNT:
        try:
            init_kwargs["login"] = int(config.MT5_ACCOUNT)
        except ValueError:
            raise RuntimeError(
                f"MT5_ACCOUNT must be an integer, got {config.MT5_ACCOUNT!r}"
            )
    if config.MT5_PASSWORD:
        init_kwargs["password"] = config.MT5_PASSWORD
    if config.MT5_SERVER:
        init_kwargs["server"] = config.MT5_SERVER

    if not mt5.initialize(**init_kwargs):
        err = mt5.last_error()
        raise RuntimeError(f"MT5 initialize failed: {err}")

    info = mt5.account_info()
    if info is None:
        mt5.shutdown()
        raise RuntimeError(
            "MT5 connected but account_info() returned None — login failed?"
        )
    log.info(
        "MT5 connected | account=%s server=%s balance=%s leverage=%s",
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
