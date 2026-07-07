from __future__ import annotations

from tradingview_mcp.core.services.coinlist import resolve_us_stock_exchange_from_coinlists
from tradingview_mcp.server import _candidate_exchanges_for_symbol


def test_local_coinlist_resolves_niq_to_nyse():
    assert resolve_us_stock_exchange_from_coinlists("NIQ") == "nyse"


def test_local_coinlist_resolves_aapl_to_nasdaq():
    assert resolve_us_stock_exchange_from_coinlists("AAPL") == "nasdaq"


def test_candidate_exchange_uses_local_coinlist_without_probe_cascade():
    bare_symbol, candidates = _candidate_exchanges_for_symbol("NIQ")
    assert bare_symbol == "NIQ"
    assert candidates == ["nyse"]
