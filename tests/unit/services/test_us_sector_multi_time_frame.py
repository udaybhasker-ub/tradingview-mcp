from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from tradingview_mcp.core.services import us_service


def test_multi_time_frame_us_sectors_returns_three_timeframes_and_sector_matrix():
    change_by_timeframe = {
        "1D": {"AMEX:XLF": 0.8, "AMEX:XLK": 1.6, "AMEX:XLV": -0.3},
        "1W": {"AMEX:XLF": 2.1, "AMEX:XLK": 3.5, "AMEX:XLV": 0.4},
        "1M": {"AMEX:XLF": 4.2, "AMEX:XLK": 6.8, "AMEX:XLV": 1.1},
    }

    def fake_get_multiple_analysis(*, screener, interval, symbols):
        assert screener == "america"
        return {
            symbol: SimpleNamespace(
                indicators={
                    "close": 100.0,
                    "volume": 1_000_000.0,
                    "RSI": 55.0,
                    "BB.upper": 110.0,
                    "BB.lower": 90.0,
                    "SMA20": 98.0,
                    "EMA50": 95.0,
                }
            )
            for symbol in symbols
            if symbol in change_by_timeframe[interval]
        }

    def fake_etf_metrics(symbol, _data):
        return {
            "symbol": symbol,
            "price": 100.0,
            "changePercent": change_by_timeframe[current_interval][symbol],
            "volume": 1_000_000.0,
            "rsi": 55.0,
            "bbw": 4.0,
            "rating": 0.7,
            "signal": "BUY",
            "bb_upper": 110.0,
            "bb_lower": 90.0,
            "sma20": 98.0,
            "ema50": 95.0,
        }

    current_interval = "1D"

    def fake_get_multiple_analysis_with_interval(*, screener, interval, symbols):
        nonlocal current_interval
        current_interval = interval
        return fake_get_multiple_analysis(screener=screener, interval=interval, symbols=symbols)

    with patch.object(us_service, "_TA_AVAILABLE", True), \
         patch.object(us_service, "get_multiple_analysis", side_effect=fake_get_multiple_analysis_with_interval), \
         patch.object(us_service, "_etf_metrics", side_effect=fake_etf_metrics):
        result = us_service.multi_time_frame_us_sectors()

    assert result["timeframes"] == ["1D", "1W", "1M"]
    assert result["heatmaps_by_timeframe"]["1D"][0]["sector"] == "technology"
    assert result["heatmaps_by_timeframe"]["1W"][0]["changePercent"] == 3.5
    assert result["sectors"][0]["sector"] == "technology"
    assert result["sectors"][0]["changes"] == {"1D": 1.6, "1W": 3.5, "1M": 6.8}

