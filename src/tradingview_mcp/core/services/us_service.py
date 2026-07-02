"""US market service — GICS sector heat via the SPDR sector ETFs.

Each sector is represented by its cap-weighted SPDR ETF proxy (XLK, XLF, ...)
rather than a hand-picked basket of constituents, matching how sector
rotation is conventionally tracked.
"""
from __future__ import annotations

from typing import List

from tradingview_mcp.core.services.indicators import compute_metrics
from tradingview_mcp.core.utils.validators import EXCHANGE_SCREENER

try:
    import tradingview_ta  # noqa: F401  presence check
    from tradingview_mcp.core.services.screener_provider import (
        resilient_get_multiple_analysis as get_multiple_analysis,
    )
    _TA_AVAILABLE = True
except ImportError:
    _TA_AVAILABLE = False


def _etf_metrics(sym: str, data) -> dict:
    ind = data.indicators
    metrics = compute_metrics(ind)
    return {
        "symbol": sym,
        "price": metrics.get("price", 0),
        "changePercent": metrics.get("change", 0),
        "volume": ind.get("volume", 0),
        "rsi": round(ind.get("RSI", 0) or 0, 2),
        "bbw": metrics.get("bbw", 0),
        "rating": metrics.get("rating", 0),
        "signal": metrics.get("signal", "N/A"),
        "bb_upper": round(ind.get("BB.upper", 0) or 0, 4),
        "bb_lower": round(ind.get("BB.lower", 0) or 0, 4),
        "sma20": round(ind.get("SMA20", 0) or 0, 4),
        "ema50": round(ind.get("EMA50", 0) or 0, 4),
    }


def multi_time_frame_us_sectors() -> dict:
    """
    Return U.S. sector heat across the core swing-investor frames: 1D, 1W, 1M.

    The payload includes per-timeframe ranked heatmaps plus a sector-centric
    matrix so downstream clients can render either tables or heatmaps without
    reshaping the response.
    """
    from tradingview_mcp.core.data.us_sectors import (
        get_all_sectors,
        get_etf_symbol,
        SECTOR_DISPLAY_NAMES,
    )

    if not _TA_AVAILABLE:
        return {"error": "tradingview_ta is missing; run `uv sync`."}

    screener = EXCHANGE_SCREENER.get("amex", "america")
    timeframes = ["1D", "1W", "1M"]
    all_sectors = get_all_sectors()
    etf_symbols = [get_etf_symbol(sector) for sector in all_sectors]

    heatmaps_by_timeframe: dict[str, List[dict]] = {}
    sectors: List[dict] = [
        {
            "sector": sector_key,
            "display_name": SECTOR_DISPLAY_NAMES.get(sector_key, sector_key),
            "etf_proxy": symbol,
            "changes": {},
        }
        for sector_key, symbol in zip(all_sectors, etf_symbols)
    ]

    for timeframe in timeframes:
        try:
            analysis = get_multiple_analysis(screener=screener, interval=timeframe, symbols=etf_symbols)
        except Exception as exc:
            return {"error": f"Analysis failed for {timeframe}: {exc}"}

        heatmap: List[dict] = []
        for sector_entry in sectors:
            data = analysis.get(sector_entry["etf_proxy"])
            if data is None:
                continue
            try:
                row = _etf_metrics(sector_entry["etf_proxy"], data)
            except Exception:
                continue
            row["sector"] = sector_entry["sector"]
            row["display_name"] = sector_entry["display_name"]
            heatmap.append(row)
            sector_entry["changes"][timeframe] = row["changePercent"]

        heatmap.sort(key=lambda x: x["changePercent"], reverse=True)
        heatmaps_by_timeframe[timeframe] = heatmap

    sectors.sort(key=lambda x: x["changes"].get("1D", float("-inf")), reverse=True)
    return {
        "market": "US",
        "timeframes": timeframes,
        "method": "SPDR sector ETF proxies",
        "available_sectors": all_sectors,
        "heatmaps_by_timeframe": heatmaps_by_timeframe,
        "sectors": sectors,
    }


def scan_us_sector(sector: str = "", timeframe: str = "1D", limit: int = 20) -> dict:
    """
    Show US GICS sector heat via SPDR sector ETF proxies (XLK, XLF, XLE, ...).

    Args:
        sector:    Sector key (empty string -> rank all 11 sector ETFs).
        timeframe: TradingView interval (default '1D').
        limit:     Unused when a single sector is requested; kept for parity
                   with the EGX sector-scan signature.

    Returns:
        Single ETF's metrics, or a heatmap ranking all sectors by change%.
    """
    from tradingview_mcp.core.data.us_sectors import (
        get_all_sectors,
        get_etf_symbol,
        SECTOR_DISPLAY_NAMES,
    )

    if not _TA_AVAILABLE:
        return {"error": "tradingview_ta is missing; run `uv sync`."}

    screener = EXCHANGE_SCREENER.get("amex", "america")

    if not sector:
        all_sectors = get_all_sectors()
        etf_symbols = [get_etf_symbol(s) for s in all_sectors]
        try:
            analysis = get_multiple_analysis(screener=screener, interval=timeframe, symbols=etf_symbols)
        except Exception as exc:
            return {"error": f"Analysis failed: {exc}"}

        heatmap: List[dict] = []
        for sector_key, sym in zip(all_sectors, etf_symbols):
            data = analysis.get(sym)
            if data is None:
                continue
            try:
                row = _etf_metrics(sym, data)
            except Exception:
                continue
            row["sector"] = sector_key
            row["display_name"] = SECTOR_DISPLAY_NAMES.get(sector_key, sector_key)
            heatmap.append(row)

        heatmap.sort(key=lambda x: x["changePercent"], reverse=True)
        return {
            "market": "US",
            "timeframe": timeframe,
            "method": "SPDR sector ETF proxies",
            "available_sectors": all_sectors,
            "heatmap": heatmap,
        }

    sector_key = sector.strip().lower().replace(" ", "_")
    etf_symbol = get_etf_symbol(sector_key)

    if not etf_symbol:
        return {
            "error": f"Unknown sector: {sector}",
            "available_sectors": get_all_sectors(),
        }

    try:
        analysis = get_multiple_analysis(screener=screener, interval=timeframe, symbols=[etf_symbol])
    except Exception as exc:
        return {"error": f"Analysis failed: {exc}"}

    data = analysis.get(etf_symbol)
    if data is None:
        return {"error": f"No data returned for {etf_symbol}"}

    try:
        row = _etf_metrics(etf_symbol, data)
    except Exception as exc:
        return {"error": f"Failed to parse {etf_symbol}: {exc}"}

    avg_change = row["changePercent"]
    return {
        "market": "US",
        "sector": sector_key,
        "display_name": SECTOR_DISPLAY_NAMES.get(sector_key, sector_key),
        "etf_proxy": etf_symbol,
        "timeframe": timeframe,
        "sector_avg_change": avg_change,
        "sector_sentiment": "Bullish" if avg_change > 0.5 else "Bearish" if avg_change < -0.5 else "Neutral",
        "data": row,
    }
