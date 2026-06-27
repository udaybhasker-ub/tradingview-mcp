"""US market GICS sector proxies via the 11 SPDR Select Sector ETFs.

Each ETF is a cap-weighted basket tracking one GICS sector of the S&P 500,
so a single liquid symbol stands in for the whole sector's performance —
the same approach used by sector-rotation commentary (e.g. "money is
rotating into XLK") rather than hand-picking individual constituents.
"""
from __future__ import annotations
from typing import Dict, List

# sector key -> (TradingView exchange prefix, ETF ticker)
SECTOR_ETFS: Dict[str, str] = {
    "technology": "AMEX:XLK",
    "health_care": "AMEX:XLV",
    "financials": "AMEX:XLF",
    "consumer_discretionary": "AMEX:XLY",
    "communication_services": "AMEX:XLC",
    "industrials": "AMEX:XLI",
    "consumer_staples": "AMEX:XLP",
    "energy": "AMEX:XLE",
    "utilities": "AMEX:XLU",
    "real_estate": "AMEX:XLRE",
    "materials": "AMEX:XLB",
}

SECTOR_DISPLAY_NAMES: Dict[str, str] = {
    "technology": "Technology",
    "health_care": "Health Care",
    "financials": "Financials",
    "consumer_discretionary": "Consumer Discretionary",
    "communication_services": "Communication Services",
    "industrials": "Industrials",
    "consumer_staples": "Consumer Staples",
    "energy": "Energy",
    "utilities": "Utilities",
    "real_estate": "Real Estate",
    "materials": "Materials",
}


def get_all_sectors() -> List[str]:
    """Return list of all available US GICS sector keys."""
    return sorted(SECTOR_ETFS.keys())


def get_etf_symbol(sector: str) -> str:
    """Return the TradingView-prefixed sector ETF symbol, or '' if unknown."""
    key = sector.lower().replace(" ", "_")
    return SECTOR_ETFS.get(key, "")
