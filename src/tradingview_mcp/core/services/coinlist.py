from __future__ import annotations
import os
from threading import Lock
from typing import Dict, List, Optional
from ..utils.validators import COINLIST_DIR


def load_symbols(exchange: str) -> List[str]:
    """Load symbols for a given exchange, with multiple fallback strategies."""
    # Try multiple possible paths
    possible_paths = [
        os.path.join(COINLIST_DIR, f"{exchange}.txt"),
        os.path.join(COINLIST_DIR, f"{exchange.lower()}.txt"),
        # Fallback: relative to this file
        os.path.join(os.path.dirname(__file__), "..", "..", "coinlist", f"{exchange}.txt"),
        # Another fallback
        os.path.join(os.path.dirname(__file__), "..", "..", "coinlist", f"{exchange.lower()}.txt")
    ]
    
    for path in possible_paths:
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    content = f.read()
                symbols = [line.strip() for line in content.split('\n') if line.strip()]
                if symbols:  # Only return if we actually got symbols
                    return symbols
        except (FileNotFoundError, IOError, UnicodeDecodeError):
            continue
    
    # If all fails, return empty list
    return []


_US_EXCHANGE_INDEX_LOCK = Lock()
_US_EXCHANGE_INDEX: Optional[Dict[str, Optional[str]]] = None
_US_STOCK_EXCHANGE_FILES = ("nasdaq", "nyse", "amex")


def _build_us_exchange_index() -> Dict[str, Optional[str]]:
    """Build a bare-symbol -> canonical exchange index from local coinlists.

    If a ticker appears on multiple U.S. lists, we mark it ambiguous with
    ``None`` so callers can fall back to a network hint or probe cascade
    instead of guessing.
    """
    index: Dict[str, Optional[str]] = {}
    for exchange in _US_STOCK_EXCHANGE_FILES:
        for full_symbol in load_symbols(exchange):
            if ":" not in full_symbol:
                continue
            _, bare_symbol = full_symbol.split(":", 1)
            bare_symbol = bare_symbol.strip().upper()
            if not bare_symbol:
                continue
            existing = index.get(bare_symbol)
            if existing is None and bare_symbol in index:
                continue
            if existing and existing != exchange:
                index[bare_symbol] = None
            elif existing is None and bare_symbol not in index:
                index[bare_symbol] = exchange
    return index


def resolve_us_stock_exchange_from_coinlists(symbol: str) -> Optional[str]:
    """Resolve a U.S. stock symbol to ``nasdaq``/``nyse``/``amex`` locally.

    Returns ``None`` if the symbol is absent from the bundled lists or appears
    on multiple lists, in which case callers should fall back to other hints.
    """
    global _US_EXCHANGE_INDEX
    bare_symbol = (symbol or "").strip().upper()
    if not bare_symbol:
        return None
    if _US_EXCHANGE_INDEX is None:
        with _US_EXCHANGE_INDEX_LOCK:
            if _US_EXCHANGE_INDEX is None:
                _US_EXCHANGE_INDEX = _build_us_exchange_index()
    return _US_EXCHANGE_INDEX.get(bare_symbol)
