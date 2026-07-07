from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tradingview_mcp.core.services import screener_service


def _fake_analysis(*, bias_by_tf: dict[str, str]):
    def fake_get_multiple_analysis(*, screener, interval, symbols):
        return {
            symbols[0]: SimpleNamespace(
                indicators={
                    "open": 100.0,
                    "close": 101.0,
                    "high": 102.0,
                    "low": 99.0,
                    "ATR": 1.5,
                    "RSI": 55.0,
                    "volume": 1_000_000.0,
                }
            )
        }

    def fake_extract_extended_indicators(_indicators):
        return {
            "rsi": 55.0,
            "macd": {"crossover": "bullish"},
            "ema": {"ema20": 100.0, "ema50": 99.0, "ema200": 95.0},
            "volume": {"signal": "normal"},
            "market_structure": {
                "trend": "uptrend",
                "trend_strength": "strong",
                "momentum_aligned": True,
            },
        }

    def fake_analyze_timeframe_context(_indicators, timeframe):
        bias = bias_by_tf[timeframe]
        return {
            "bias": bias,
            "bias_reasons": [f"{timeframe} bias is {bias.lower()}"],
            "key_indicators_for_timeframe": ["RSI", "EMA", "MACD"],
            "advice": f"Use {timeframe} for {bias.lower()} context.",
        }

    return fake_get_multiple_analysis, fake_extract_extended_indicators, fake_analyze_timeframe_context


def test_multi_timeframe_analysis_includes_monthly_frame_and_rules():
    bias_by_tf = {
        "1M": "Bullish",
        "1W": "Bullish",
        "1D": "Bullish",
        "4h": "Bullish",
        "1h": "Bullish",
        "15m": "Bullish",
    }
    fake_get_multiple_analysis, fake_extract_extended_indicators, fake_analyze_timeframe_context = _fake_analysis(
        bias_by_tf=bias_by_tf
    )

    with patch.object(screener_service, "_TA_AVAILABLE", True), \
         patch("tradingview_mcp.core.utils.validators.resolve_screener_for_symbol", return_value="america"), \
         patch.object(screener_service, "get_multiple_analysis", side_effect=fake_get_multiple_analysis), \
         patch.object(screener_service, "compute_metrics", return_value={"price": 101.0, "change": 1.0}), \
         patch("tradingview_mcp.core.services.indicators.extract_extended_indicators", side_effect=fake_extract_extended_indicators), \
         patch("tradingview_mcp.core.services.indicators.analyze_timeframe_context", side_effect=fake_analyze_timeframe_context):
        result = screener_service.run_multi_timeframe_analysis(
            "NASDAQ:NVDA", "NASDAQ", ["1M", "1W", "1D", "4h", "1h", "15m"]
        )

    assert list(result["timeframes"]) == ["1M", "1W", "1D", "4h", "1h", "15m"]
    assert result["timeframes"]["1M"]["label"] == "Monthly (Macro Trend)"
    assert result["alignment"]["status"] == "FULLY ALIGNED BULLISH"
    assert result["alignment"]["scores_by_tf"]["1M"] == 1
    assert result["recommendation"]["rules"][0] == "Monthly sets MACRO trend and regime"
    assert result["recommendation"]["rules"][-1] == "Never trade against Monthly + Weekly combined direction"


def test_multi_timeframe_analysis_keeps_three_vs_three_split_mixed():
    bias_by_tf = {
        "1M": "Bullish",
        "1W": "Bullish",
        "1D": "Bullish",
        "4h": "Bearish",
        "1h": "Bearish",
        "15m": "Bearish",
    }
    fake_get_multiple_analysis, fake_extract_extended_indicators, fake_analyze_timeframe_context = _fake_analysis(
        bias_by_tf=bias_by_tf
    )

    with patch.object(screener_service, "_TA_AVAILABLE", True), \
         patch("tradingview_mcp.core.utils.validators.resolve_screener_for_symbol", return_value="america"), \
         patch.object(screener_service, "get_multiple_analysis", side_effect=fake_get_multiple_analysis), \
         patch.object(screener_service, "compute_metrics", return_value={"price": 101.0, "change": 1.0}), \
         patch("tradingview_mcp.core.services.indicators.extract_extended_indicators", side_effect=fake_extract_extended_indicators), \
         patch("tradingview_mcp.core.services.indicators.analyze_timeframe_context", side_effect=fake_analyze_timeframe_context):
        result = screener_service.run_multi_timeframe_analysis(
            "NASDAQ:NVDA", "NASDAQ", ["1M", "1W", "1D", "4h", "1h", "15m"]
        )

    assert result["alignment"]["net_score"] == 0
    assert result["alignment"]["status"] == "MIXED/RANGING"
    assert result["alignment"]["divergent_timeframes"] == ["4h", "1h", "15m"]


def test_multi_timeframe_analysis_reorders_subset_by_precedence():
    # Caller asks for 1M, 1H, 1D (in that order) — response must come back
    # reordered to canonical precedence: 1M, 1D, 1H.
    bias_by_tf = {"1M": "Bullish", "1D": "Bullish", "1h": "Bearish"}
    fake_get_multiple_analysis, fake_extract_extended_indicators, fake_analyze_timeframe_context = _fake_analysis(
        bias_by_tf=bias_by_tf
    )

    with patch.object(screener_service, "_TA_AVAILABLE", True), \
         patch("tradingview_mcp.core.utils.validators.resolve_screener_for_symbol", return_value="america"), \
         patch.object(screener_service, "get_multiple_analysis", side_effect=fake_get_multiple_analysis), \
         patch.object(screener_service, "compute_metrics", return_value={"price": 101.0, "change": 1.0}), \
         patch("tradingview_mcp.core.services.indicators.extract_extended_indicators", side_effect=fake_extract_extended_indicators), \
         patch("tradingview_mcp.core.services.indicators.analyze_timeframe_context", side_effect=fake_analyze_timeframe_context):
        result = screener_service.run_multi_timeframe_analysis("NASDAQ:NVDA", "NASDAQ", ["1M", "1H", "1D"])

    assert list(result["timeframes"].keys()) == ["1M", "1D", "1h"]
    assert list(result["alignment"]["scores_by_tf"].keys()) == ["1M", "1D", "1h"]


def test_multi_timeframe_analysis_requires_timeframes():
    with pytest.raises(ValueError):
        screener_service.run_multi_timeframe_analysis("NASDAQ:NVDA", "NASDAQ", [])


def test_multi_timeframe_analysis_rejects_invalid_timeframe():
    with pytest.raises(ValueError):
        screener_service.run_multi_timeframe_analysis("NASDAQ:NVDA", "NASDAQ", ["1M", "banana"])
