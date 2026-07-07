from __future__ import annotations
import os
from typing import Set

ALLOWED_TIMEFRAMES: Set[str] = {"5m", "15m", "1h", "4h", "1D", "1W", "1M"}
_TIMEFRAME_ALIASES = {
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1D",
    "1w": "1W",
    "1m": "1M",
}

# Exchanges that represent stock markets (not crypto)
STOCK_EXCHANGES: Set[str] = {
    "nasdaq", "nyse",
    "amex", "nysearca", "pcx",  # NYSE Arca / AMEX ETF aliases
}

EXCHANGE_SCREENER = {
    "all": "crypto",
    "huobi": "crypto",
    "kucoin": "crypto",
    "coinbase": "crypto",
    "gateio": "crypto",
    "binance": "crypto",
    "bitfinex": "crypto",
    "bitget": "crypto",
    "bybit": "crypto",
    "okx": "crypto",
    "mexc": "crypto",
    "nasdaq": "america",
    "nyse": "america",
    # NYSE Arca / AMEX — ETFs (GDX, GLD, XLE, SPY, QQQ …) are listed here in TradingView
    "amex": "america",      # TradingView canonical prefix for NYSE Arca ETFs
    "nysearca": "america",  # alias: NYSE Arca (official name used by issuers)
    "pcx": "america",       # alias: Pacific Exchange (historical MIC code for NYSE Arca)
}

# Venues TradingView serves for single-symbol TA (tradingview-ta) but NOT via the
# scanner (tradingview-screener returns 0 rows for the "forex"/"cfd" markets).
# Kept SEPARATE from EXCHANGE_SCREENER on purpose: scanner / multi-timeframe paths
# read EXCHANGE_SCREENER through get_market_type(), so isolating these here means
# those paths keep their existing behaviour and never query an unsupported market.
_TA_ONLY_SCREENERS: dict = {
    # Forex (currency pairs): EUR/USD, GBP/USD, USD/TRY, USD/JPY …
    "oanda": "forex",
    "fx_idc": "forex",
    "fxcm": "forex",
    # CFD: spot metals (GOLD, SILVER), indices (DXY, SPX) …
    "tvc": "cfd",
    "capitalcom": "cfd",
}

# Map validated exchange identifiers to their canonical TradingView symbol prefix.
# TradingView uses "AMEX" as the prefix for all NYSE Arca / ETF listings; passing
# "NYSE:GDX" returns no data even though GDX trades on NYSE Arca.
_EXCHANGE_TV_PREFIX: dict = {
    "amex": "AMEX",
    "nysearca": "AMEX",
    "pcx": "AMEX",
    "nasdaq": "NASDAQ",
    "nyse": "NYSE",
}

_YAHOO_SYMBOL_ALIASES: dict = {
}

_TRADINGVIEW_SYMBOL_ALIASES: dict = {
    # Spot metals in forex-pair notation — unambiguous (no venue lists a stock or
    # token called "XAUUSD"/"XAGUSD"), so these always map to the TVC CFD feed.
    "XAUUSD": "TVC:GOLD",
    "XAGUSD": "TVC:SILVER",
}

# "Soft" commodity aliases: bare tickers that ALSO exist as real equities/indices
# (e.g. NYSE:GOLD is Barrick Gold Corp). normalize_tradingview_symbol() applies
# these ONLY when the caller is not targeting a stock exchange — so NYSE:GOLD keeps
# resolving to the equity, while a crypto/default context maps "gold" to spot gold.
_COMMODITY_SOFT_ALIASES: dict = {
    "GOLD": "TVC:GOLD",
    "XAU": "TVC:GOLD",
    "SILVER": "TVC:SILVER",
    "XAG": "TVC:SILVER",
    "DXY": "TVC:DXY",
}


def get_tv_exchange_prefix(exchange: str) -> str:
    """Return the TradingView symbol prefix for *exchange* (e.g. ``AMEX`` for ``nysearca``).

    Falls back to ``exchange.upper()`` for exchanges not in the explicit map so
    that unlisted venues still preserve their TradingView-style prefix.
    """
    return _EXCHANGE_TV_PREFIX.get(exchange.strip().lower(), exchange.upper())


def normalize_yahoo_symbol(symbol: str) -> str:
    """Return a provider-specific Yahoo Finance symbol for common user aliases."""
    raw = (symbol or "").strip().upper()
    return _YAHOO_SYMBOL_ALIASES.get(raw, raw)


def normalize_tradingview_symbol(symbol: str, exchange: str) -> str:
    """Return a fully-qualified TradingView symbol, resolving common index aliases."""
    raw = (symbol or "").strip().upper()
    if raw in _TRADINGVIEW_SYMBOL_ALIASES:
        return _TRADINGVIEW_SYMBOL_ALIASES[raw]
    if ":" in raw:
        return raw
    # Soft commodity aliases collide with real equities (NYSE:GOLD = Barrick Gold),
    # so only apply them for non-stock venues; stock exchanges keep the literal.
    if raw in _COMMODITY_SOFT_ALIASES and not is_stock_exchange(exchange):
        return _COMMODITY_SOFT_ALIASES[raw]
    return f"{get_tv_exchange_prefix(exchange)}:{raw}"

# Get absolute path to coinlist directory relative to this module
# This file is at: src/tradingview_mcp/core/utils/validators.py
# We want: src/tradingview_mcp/coinlist/
_this_file = __file__
_utils_dir = os.path.dirname(_this_file)  # core/utils
_core_dir = os.path.dirname(_utils_dir)   # core  
_package_dir = os.path.dirname(_core_dir) # tradingview_mcp
COINLIST_DIR = os.path.join(_package_dir, 'coinlist')


def sanitize_timeframe(tf: str, default: str = "5m") -> str:
    if not tf:
        return default
    normalized = tf.strip().lower()
    return _TIMEFRAME_ALIASES.get(normalized, default)


# Canonical precedence order for multi-timeframe alignment analysis: macro
# (Monthly) down to execution (5m). Output ordering always follows this order
# regardless of the order the caller listed timeframes in.
TIMEFRAME_PRECEDENCE: list = ["1M", "1W", "1D", "4h", "1h", "15m", "5m"]


def normalize_timeframe_list(timeframes) -> list:
    """Validate a caller-supplied list of timeframes and reorder by precedence.

    Unlike ``sanitize_timeframe``, invalid values raise ``ValueError`` instead
    of silently falling back to a default — callers of multi-timeframe
    analysis must pass timeframes explicitly, so a typo should surface rather
    than get swallowed.

    Args:
        timeframes: Non-empty list of timeframe strings (case-insensitive,
            e.g. "1H", "1d", "15M"). Duplicates are collapsed.

    Returns:
        The requested timeframes, deduplicated and reordered to
        ``TIMEFRAME_PRECEDENCE`` order (Monthly -> Weekly -> Daily -> 4H ->
        1H -> 15m -> 5m).

    Raises:
        ValueError: if ``timeframes`` is empty/None or contains a value that
            doesn't map to a known timeframe.
    """
    if not timeframes:
        raise ValueError("timeframes is required and must contain at least one value")

    normalized: set = set()
    for tf in timeframes:
        key = (tf or "").strip().lower()
        canonical = _TIMEFRAME_ALIASES.get(key)
        if canonical is None:
            raise ValueError(
                f"invalid timeframe: {tf!r}; expected one of {sorted(ALLOWED_TIMEFRAMES)}"
            )
        normalized.add(canonical)

    return [tf for tf in TIMEFRAME_PRECEDENCE if tf in normalized]


def sanitize_exchange(ex: str, default: str = "kucoin") -> str:
    if not ex:
        return default
    exs = ex.strip().lower()
    if exs in EXCHANGE_SCREENER or exs in _TA_ONLY_SCREENERS:
        return exs
    return default


def is_stock_exchange(exchange: str) -> bool:
    """Return True if the exchange is a stock market (not crypto)."""
    return exchange.strip().lower() in STOCK_EXCHANGES


def get_market_type(exchange: str) -> str:
    """Return the TradingView market type for screener queries."""
    return EXCHANGE_SCREENER.get(exchange.strip().lower(), "crypto")


def resolve_screener_for_symbol(full_symbol: str, exchange: str) -> str:
    """Pick the TradingView screener for an already-resolved symbol.

    A single venue can host assets needing *different* screeners
    (``OANDA:EURUSD`` → ``forex`` but ``OANDA:XAUUSD`` → ``cfd``), and symbol
    aliases can redirect to another venue (``XAUUSD`` → ``TVC:GOLD``). So the
    screener must follow the *final* symbol's prefix, not the exchange the
    caller originally passed. Falls back to the exchange's screener (then
    ``crypto``) when the symbol carries no explicit prefix.
    """
    prefix = (full_symbol.split(":", 1)[0] if ":" in full_symbol
              else (exchange or "")).strip().lower()
    return (EXCHANGE_SCREENER.get(prefix)
            or _TA_ONLY_SCREENERS.get(prefix)
            or "crypto")
