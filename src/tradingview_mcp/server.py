"""
TradingView MCP Server — routing layer only.

Each @mcp.tool() handler is responsible for:
  1. Validating / sanitising parameters
  2. Delegating to the appropriate service module
  3. Returning the result

No business logic lives here. All computation is in core/services/*.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
from typing import Any, Optional

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from tradingview_mcp.core.auth import OAuthError, SharedSecretOAuthServer, render_authorize_form

# ── Service imports ────────────────────────────────────────────────────────────
from tradingview_mcp.core.services.screener_service import (
    fetch_bollinger_analysis,
    fetch_trending_analysis,
    analyze_asset,
    scan_consecutive_candles,
    scan_advanced_candle_patterns_single_tf,
    fetch_multi_timeframe_patterns,
    run_multi_timeframe_analysis,
)
from tradingview_mcp.core.services.scanner_service import (
    volume_breakout_scan,
    volume_confirmation_analyze,
    smart_volume_scan,
)
from tradingview_mcp.core.services.multi_agent_service import run_multi_agent_analysis
from tradingview_mcp.core.services.coinlist import load_symbols
from tradingview_mcp.core.services.us_service import scan_us_sector
from tradingview_mcp.core.services.news_service import fetch_news_summary
from tradingview_mcp.core.services.yahoo_finance_service import (
    get_price,
    get_market_snapshot,
)
from tradingview_mcp.core.services.extended_hours_service import get_extended_hours_price
from tradingview_mcp.core.services.options_service import (
    get_options_chain,
    get_unusual_options_activity,
)
from tradingview_mcp.core.services.backtest_service import (
    run_backtest,
    compare_strategies as _compare_strategies,
    walk_forward_backtest,
)
from tradingview_mcp.core.utils.validators import (
    sanitize_timeframe,
    sanitize_exchange,
    normalize_tradingview_symbol,
    normalize_yahoo_symbol,
)
from tradingview_mcp.core.errors import (
    BatchExecutionError,
    ErrorCode,
    make_error,
)

try:
    import tradingview_screener  # noqa: F401
    TRADINGVIEW_SCREENER_AVAILABLE = True
except ImportError:
    TRADINGVIEW_SCREENER_AVAILABLE = False


# ── MCP server instance ────────────────────────────────────────────────────────

def _build_auth() -> tuple[SharedSecretOAuthServer | None, AuthSettings | None]:
    """Configure OAuth from env vars, if MCP_AUTH_TOKEN is set.

    Claude.ai's remote connector flow requires an OAuth handshake even for
    single-user / shared-secret setups. When MCP_AUTH_TOKEN is unset, the
    server runs unauthenticated (e.g. local development).
    """
    token = os.environ.get("MCP_AUTH_TOKEN")
    if not token:
        return None, None

    public_url = os.environ.get("MCP_PUBLIC_URL")
    if not public_url:
        raise RuntimeError(
            "MCP_AUTH_TOKEN is set but MCP_PUBLIC_URL is not. "
            "MCP_PUBLIC_URL must be the public https URL of this deployment "
            "(e.g. https://your-app.up.railway.app)."
        )
    if "://" not in public_url:
        public_url = f"https://{public_url}"

    auth_server = SharedSecretOAuthServer(token, "TradingView Multi-Market Screener")
    settings = AuthSettings(
        issuer_url=AnyHttpUrl(public_url),
        resource_server_url=AnyHttpUrl(f"{public_url.rstrip('/')}/mcp"),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["*"],
            default_scopes=["*"],
        ),
    )
    return auth_server, settings


_auth_provider, _auth_settings = _build_auth()

mcp = FastMCP(
    name="TradingView Multi-Market Screener",
    instructions=(
        "U.S. market screener backed by TradingView and Yahoo Finance. "
        "Focuses on NASDAQ, NYSE, and AMEX-listed stocks and ETFs. "
        "Tools: top_gainers, top_losers, bollinger_scan, asset_analysis, multi_agent_analysis, "
        "volume_breakout_scanner, us_sector_scan, stock_extended_hours, and options tools."
    ),
    token_verifier=_auth_provider,
    auth=_auth_settings,
)


_REST_ROUTES = {
    "GET /api/assets/{symbol}/analysis": "Preferred alias for single-asset technical analysis. Optional query: ?exchange=NASDAQ",
    "GET /api/assets/{symbol}/multi-agent-analysis": "Three-agent technical/sentiment/risk debate. Optional query: ?exchange=NASDAQ",
    "GET /api/assets/{symbol}/multi-timeframe-analysis": "Monthly to intraday alignment. Optional query: ?exchange=NASDAQ",
    "GET /api/assets/{symbol}/volume-confirmation": "Single-asset volume confirmation analysis. Optional query: ?exchange=NASDAQ",
    "GET /api/markets/{exchange}/gainers": "Top movers by exchange.",
    "GET /api/markets/{exchange}/losers": "Top losers by exchange.",
    "GET /api/markets/{exchange}/bollinger-scan": "Bollinger squeeze scan.",
    "GET /api/markets/{exchange}/rating-filter": "Bollinger rating filter.",
    "GET /api/markets/{exchange}/consecutive-candles": "Consecutive candle pattern scan.",
    "GET /api/markets/{exchange}/advanced-candle-pattern": "Advanced candle-pattern scan.",
    "GET /api/markets/{exchange}/volume-breakouts": "Volume breakout scan.",
    "GET /api/markets/{exchange}/smart-volume": "Volume + RSI scan.",
    "GET /api/markets/us/sectors": "U.S. sector ETF heatmap.",
    "GET /api/news": "RSS financial news summary.",
    "GET /api/yahoo/price/{symbol}": "Yahoo quote endpoint.",
    "GET /api/yahoo/market-snapshot": "Indices, FX, and ETF snapshot.",
    "GET /api/stocks/{symbol}/extended-hours": "U.S. pre/post-market pricing.",
    "GET /api/stocks/{symbol}/options-chain": "Options chain for one expiry.",
    "GET /api/stocks/{symbol}/options-unusual-activity": "Top V/OI contracts.",
    "POST /api/backtests/run": "Run one strategy backtest.",
    "POST /api/backtests/compare": "Compare all strategies.",
    "POST /api/backtests/walk-forward": "Walk-forward validation.",
}

_REQUIRED = object()
_COMMON_AMEX_SYMBOLS = {
    "SPY", "QQQ", "DIA", "IWM",
    "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
    "GLD", "SLV", "GDX", "GDXJ", "ARKK", "SMH",
}
_DEFAULT_US_STOCK_EXCHANGE_CANDIDATES = ["nasdaq", "nyse", "amex"]
_ALLOWED_US_EXCHANGES = {"nasdaq", "nyse", "amex", "nysearca", "pcx"}


def _json_response(payload: Any, status_code: int = 200) -> Response:
    return Response(
        content=json.dumps(payload, allow_nan=True, default=str),
        status_code=status_code,
        media_type="application/json",
    )


def _cors_json_response(payload: Any, status_code: int = 200) -> Response:
    response = _json_response(payload, status_code=status_code)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, MCP-Protocol-Version"
    return response


def _issuer_url(request) -> str:
    public_url = os.environ.get("MCP_PUBLIC_URL")
    if public_url:
        if "://" not in public_url:
            public_url = f"https://{public_url}"
        return public_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def _resource_metadata_url(request) -> str:
    return f"{_issuer_url(request)}/.well-known/oauth-protected-resource"


def _auth_error(detail: str, status_code: int = 401) -> Response:
    response = _json_response({"error": detail}, status_code=status_code)
    if _auth_settings is not None:
        response.headers["WWW-Authenticate"] = f'Bearer resource_metadata="{_resource_metadata_url_placeholder()}"'
    else:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


def _auth_error_for_request(request, detail: str, status_code: int = 401) -> Response:
    response = _json_response({"error": detail}, status_code=status_code)
    if _auth_settings is not None:
        response.headers["WWW-Authenticate"] = f'Bearer resource_metadata="{_resource_metadata_url(request)}"'
    else:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


def _resource_metadata_url_placeholder() -> str:
    if _auth_settings is None:
        return ""
    return f"{str(_auth_settings.issuer_url).rstrip('/')}/.well-known/oauth-protected-resource"


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value}")


def _coerce_param(name: str, value: Any, kind: type) -> Any:
    if value is None:
        return None
    if kind is bool:
        return _coerce_bool(value)
    if kind is int:
        return int(value)
    if kind is float:
        return float(value)
    if kind is str:
        return str(value)
    raise ValueError(f"unsupported parameter type for {name}: {kind}")


def _normalize_exchange_override(exchange: str | None) -> str | None:
    if exchange is None:
        return None
    return _sanitize_us_exchange(exchange)


def _sanitize_us_exchange(exchange: str, default: str = "NASDAQ") -> str:
    normalized = sanitize_exchange(exchange, default)
    if normalized not in _ALLOWED_US_EXCHANGES:
        raise ValueError(
            f"unsupported exchange: {exchange}. "
            "This server is focused on U.S. markets and only accepts NASDAQ, NYSE, AMEX, NYSEARCA, or PCX."
        )
    return normalized


def _strip_symbol_exchange_prefix(symbol: str) -> tuple[str | None, str]:
    raw_symbol = (symbol or "").strip().upper()
    if not raw_symbol:
        raise ValueError("symbol is required")

    if ":" in raw_symbol:
        prefix, bare_symbol = raw_symbol.split(":", 1)
        normalized = sanitize_exchange(prefix, "")
        if not normalized:
            raise ValueError(f"unsupported exchange prefix in symbol: {prefix}")
        return normalized, bare_symbol

    return None, raw_symbol


def _candidate_exchanges_for_symbol(symbol: str, exchange_override: str | None = None) -> tuple[str, list[str]]:
    prefixed_exchange, bare_symbol = _strip_symbol_exchange_prefix(symbol)
    if exchange_override:
        return bare_symbol, [exchange_override]
    if prefixed_exchange:
        if prefixed_exchange not in _ALLOWED_US_EXCHANGES:
            raise ValueError(
                f"unsupported exchange prefix: {prefixed_exchange}. "
                "This server is focused on U.S. markets and only accepts NASDAQ, NYSE, AMEX, NYSEARCA, or PCX."
            )
        return bare_symbol, [prefixed_exchange]
    if bare_symbol in _COMMON_AMEX_SYMBOLS:
        return bare_symbol, ["amex", "nasdaq", "nyse"]
    return bare_symbol, _DEFAULT_US_STOCK_EXCHANGE_CANDIDATES


def _is_exchange_miss_error(result: dict[str, Any]) -> bool:
    error = str(result.get("error", "")).lower()
    return any(
        needle in error
        for needle in (
            "no data found",
            "no indicator data",
            "could not compute metrics",
        )
    )


def _resolve_exchange_for_asset_routes(
    symbol: str,
    timeframe: str,
    exchange_override: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    bare_symbol, candidates = _candidate_exchanges_for_symbol(symbol, exchange_override)

    if len(candidates) == 1:
        return candidates[0], None

    attempted_exchanges: list[str] = []
    last_error: dict[str, Any] | None = None
    for exchange in candidates:
        attempted_exchanges.append(exchange)
        probe = analyze_asset(bare_symbol, exchange, timeframe)
        if not isinstance(probe, dict) or "error" not in probe:
            return exchange, probe
        last_error = probe
        if not _is_exchange_miss_error(probe):
            break

    raise ValueError(
        json.dumps(
            {
                "error": "exchange_inference_failed",
                "symbol": bare_symbol,
                "attempted_exchanges": attempted_exchanges,
                "suggestion": "retry with ?exchange=NASDAQ, ?exchange=NYSE, or ?exchange=AMEX",
                "last_error": last_error,
            },
            default=str,
        )
    )


async def _resolve_asset_route(
    request,
    timeframe_default: str,
    handler,
    include_timeframe: bool = True,
) -> Response:
    auth_error = _require_bearer_auth(request)
    if auth_error is not None:
        return auth_error
    try:
        raw_symbol = request.path_params["symbol"]
        timeframe = request.query_params.get("timeframe", timeframe_default)
        exchange_override = _normalize_exchange_override(request.query_params.get("exchange"))
        bare_symbol, _ = _candidate_exchanges_for_symbol(raw_symbol, exchange_override)
        exchange, probe_result = _resolve_exchange_for_asset_routes(
            raw_symbol,
            timeframe,
            exchange_override=exchange_override,
        )
        if handler is asset_analysis and probe_result is not None:
            return _json_response(probe_result)
        params = {"symbol": bare_symbol, "exchange": exchange}
        if include_timeframe:
            params["timeframe"] = timeframe
        return _json_response(handler(**params))
    except ValueError as exc:
        try:
            return _json_response(json.loads(str(exc)), status_code=400)
        except Exception:
            return _json_response({"error": str(exc)}, status_code=400)


async def _extract_params(request, specs: dict[str, tuple[type, Any]]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if request.method in {"POST", "PUT", "PATCH"}:
        try:
            parsed = await request.json()
            if isinstance(parsed, dict):
                body = parsed
        except Exception:
            body = {}

    params: dict[str, Any] = {}
    for name, (kind, default) in specs.items():
        if name in body:
            raw = body[name]
        else:
            raw = request.query_params.get(name, default)
        if raw is _REQUIRED:
            raise ValueError(f"missing required parameter: {name}")
        params[name] = _coerce_param(name, raw, kind) if raw is not None else None
    return params


def _require_bearer_auth(request) -> Response | None:
    token = os.environ.get("MCP_AUTH_TOKEN")
    if not token:
        return None

    header = request.headers.get("authorization", "")
    scheme, _, provided = header.partition(" ")
    if scheme.lower() != "bearer" or not provided:
        return _auth_error_for_request(request, "missing bearer token")
    provided_token = provided.strip()
    if _auth_provider is not None and _auth_provider.is_valid_bearer_token(provided_token):
        return None
    return _auth_error_for_request(request, "invalid bearer token", status_code=403)


async def _run_rest_handler(
    request,
    handler,
    specs: dict[str, tuple[type, Any]] | None = None,
    path_params: dict[str, Any] | None = None,
) -> Response:
    auth_error = _require_bearer_auth(request)
    if auth_error is not None:
        return auth_error

    try:
        params = await _extract_params(request, specs or {})
        if path_params:
            params.update(path_params)
        return _json_response(handler(**params))
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return _json_response({"error": f"REST handler failed: {exc}"}, status_code=500)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return PlainTextResponse("OK")


@mcp.custom_route("/api/routes", methods=["GET"])
async def rest_routes(request):
    return _json_response(
        {
            "auth": "Authorization: Bearer <MCP_AUTH_TOKEN>" if os.environ.get("MCP_AUTH_TOKEN") else "disabled",
            "routes": _REST_ROUTES,
        }
    )


if _auth_provider is not None and _auth_settings is not None:
    def _authorization_server_metadata(request) -> dict[str, Any]:
        issuer = _issuer_url(request)
        return {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
            "registration_endpoint": f"{issuer}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
        }


    def _protected_resource_metadata(request) -> dict[str, Any]:
        issuer = _issuer_url(request)
        return {
            "resource": f"{issuer}/mcp",
            "authorization_servers": [issuer],
            "bearer_methods_supported": ["header"],
        }


    @mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET", "OPTIONS"])
    async def oauth_authorization_server_metadata(request):
        if request.method == "OPTIONS":
            return _cors_json_response({})
        return _cors_json_response(_authorization_server_metadata(request))


    @mcp.custom_route("/.well-known/openid-configuration", methods=["GET", "OPTIONS"])
    async def openid_configuration(request):
        if request.method == "OPTIONS":
            return _cors_json_response({})
        return _cors_json_response(_authorization_server_metadata(request))


    @mcp.custom_route("/.well-known/oauth-protected-resource/mcp", methods=["GET", "OPTIONS"])
    async def protected_resource_metadata_mcp(request):
        if request.method == "OPTIONS":
            return _cors_json_response({})
        return _cors_json_response(_protected_resource_metadata(request))


    @mcp.custom_route("/register", methods=["POST", "OPTIONS"])
    async def oauth_register(request):
        if request.method == "OPTIONS":
            return _cors_json_response({})
        try:
            body = await request.json()
        except Exception:
            body = {}
        redirect_uris = body.get("redirect_uris")
        if not isinstance(redirect_uris, list):
            return _cors_json_response(
                {
                    "error": "invalid_client_metadata",
                    "error_description": "redirect_uris is required",
                },
                status_code=400,
            )
        redirect_uris = [uri for uri in redirect_uris if isinstance(uri, str)]
        try:
            client = _auth_provider.register_client(redirect_uris, body.get("client_name"))
        except OAuthError as exc:
            return _cors_json_response({"error": exc.error, "error_description": exc.description}, status_code=exc.status_code)
        return _cors_json_response(
            {
                "client_id": client.client_id,
                "redirect_uris": [str(uri) for uri in client.redirect_uris],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
            status_code=201,
        )


    @mcp.custom_route("/authorize", methods=["GET", "POST"])
    async def oauth_authorize(request):
        if request.method == "GET":
            params = _auth_provider.extract_authorize_params(dict(request.query_params))
            try:
                _auth_provider.validate_authorize_request(params)
            except OAuthError as exc:
                return _json_response({"error": exc.error, "error_description": exc.description}, status_code=exc.status_code)
            return HTMLResponse(render_authorize_form(mcp.name, params))

        form = await request.form()
        raw_form = dict(form)
        params = _auth_provider.extract_authorize_params(raw_form)
        try:
            _auth_provider.validate_authorize_request(params)
        except OAuthError as exc:
            return _json_response({"error": exc.error, "error_description": exc.description}, status_code=exc.status_code)

        submitted_token = raw_form.get("token")
        if not isinstance(submitted_token, str) or not secrets.compare_digest(submitted_token, os.environ["MCP_AUTH_TOKEN"]):
            return HTMLResponse(render_authorize_form(mcp.name, params, "Incorrect token. Try again."), status_code=401)

        redirect_url = _auth_provider.create_authorization_redirect(params)
        return RedirectResponse(redirect_url, status_code=302, headers={"Cache-Control": "no-store"})


    @mcp.custom_route("/token", methods=["POST", "OPTIONS"])
    async def oauth_token(request):
        if request.method == "OPTIONS":
            return _cors_json_response({})

        form = await request.form()
        grant_type = form.get("grant_type")
        client_id = form.get("client_id")
        if not isinstance(grant_type, str) or not isinstance(client_id, str):
            return _cors_json_response({"error": "invalid_request", "error_description": "grant_type and client_id are required"}, status_code=400)

        try:
            if grant_type == "authorization_code":
                code = form.get("code")
                redirect_uri = form.get("redirect_uri")
                code_verifier = form.get("code_verifier")
                if not all(isinstance(value, str) for value in (code, redirect_uri, code_verifier)):
                    raise OAuthError("invalid_request", "code, redirect_uri, and code_verifier are required")
                payload = _auth_provider.exchange_code(
                    client_id=client_id,
                    code=code,
                    redirect_uri=redirect_uri,
                    code_verifier=code_verifier,
                )
            elif grant_type == "refresh_token":
                refresh_token = form.get("refresh_token")
                if not isinstance(refresh_token, str):
                    raise OAuthError("invalid_request", "refresh_token is required")
                payload = _auth_provider.exchange_refresh_token(client_id=client_id, refresh_token=refresh_token)
            else:
                raise OAuthError("unsupported_grant_type", "Only authorization_code and refresh_token are supported")
        except OAuthError as exc:
            return _cors_json_response({"error": exc.error, "error_description": exc.description}, status_code=400)

        response = _cors_json_response(payload)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response

# ── Screener tools ─────────────────────────────────────────────────────────────

@mcp.tool()
def top_gainers(exchange: str = "NASDAQ", timeframe: str = "15m", limit: int = 25) -> list[dict] | dict:
    """Return top gainers for an exchange and timeframe using Bollinger Band analysis.

    Args:
        exchange: Exchange name — NASDAQ, NYSE, or AMEX
        timeframe: One of 5m, 15m, 1h, 4h, 1D, 1W, 1M
        limit: Number of rows to return (max 50)

    Returns:
        list[dict] on success. On total upstream failure returns a structured
        error envelope: ``{"error": {"code": "ALL_BATCHES_FAILED", ...}}``.
    """
    exchange = _sanitize_us_exchange(exchange, "NASDAQ")
    timeframe = sanitize_timeframe(timeframe, "15m")
    limit = max(1, min(limit, 50))
    try:
        rows = fetch_trending_analysis(exchange, timeframe=timeframe, limit=limit)
    except BatchExecutionError as e:
        return make_error(
            ErrorCode.ALL_BATCHES_FAILED, str(e),
            batches_attempted=e.batches_attempted,
            batches_failed=e.batches_failed,
            first_error=e.first_error,
        )
    return [{"symbol": r["symbol"], "changePercent": r["changePercent"], "indicators": dict(r["indicators"])} for r in rows]


@mcp.tool()
def top_losers(exchange: str = "NASDAQ", timeframe: str = "15m", limit: int = 25) -> list[dict] | dict:
    """Return top losers for a U.S. exchange and timeframe.

    Returns ``list[dict]`` on success, or an error envelope on total upstream
    failure (``{"error": {"code": "ALL_BATCHES_FAILED", ...}}``).
    """
    exchange = _sanitize_us_exchange(exchange, "NASDAQ")
    timeframe = sanitize_timeframe(timeframe, "15m")
    limit = max(1, min(limit, 50))
    try:
        rows = fetch_trending_analysis(exchange, timeframe=timeframe, limit=limit)
    except BatchExecutionError as e:
        return make_error(
            ErrorCode.ALL_BATCHES_FAILED, str(e),
            batches_attempted=e.batches_attempted,
            batches_failed=e.batches_failed,
            first_error=e.first_error,
        )
    rows.sort(key=lambda x: x["changePercent"])
    return [{"symbol": r["symbol"], "changePercent": r["changePercent"], "indicators": dict(r["indicators"])} for r in rows[:limit]]


@mcp.tool()
def bollinger_scan(exchange: str = "NASDAQ", timeframe: str = "4h", bbw_threshold: float = 0.04, limit: int = 50) -> list[dict]:
    """Scan U.S. stocks and ETFs for low Bollinger Band Width (squeeze detection).

    Args:
        exchange: Exchange — NASDAQ, NYSE, or AMEX
        timeframe: One of 5m, 15m, 1h, 4h, 1D, 1W, 1M
        bbw_threshold: Maximum BBW value to filter (default 0.04)
        limit: Number of rows to return (max 100)
    """
    exchange = _sanitize_us_exchange(exchange, "NASDAQ")
    timeframe = sanitize_timeframe(timeframe, "4h")
    limit = max(1, min(limit, 100))
    rows = fetch_bollinger_analysis(exchange, timeframe=timeframe, bbw_filter=bbw_threshold, limit=limit)
    return [{"symbol": r["symbol"], "changePercent": r["changePercent"], "indicators": dict(r["indicators"])} for r in rows]


@mcp.tool()
def rating_filter(exchange: str = "NASDAQ", timeframe: str = "5m", rating: int = 2, limit: int = 25) -> list[dict] | dict:
    """Filter assets by Bollinger Band rating.

    Args:
        exchange: Exchange name like NASDAQ, NYSE, or AMEX
        timeframe: One of 5m, 15m, 1h, 4h, 1D, 1W, 1M
        rating: BB rating (-3 to +3): -3=Strong Sell, -2=Sell, -1=Weak Sell, 1=Weak Buy, 2=Buy, 3=Strong Buy
        limit: Number of rows to return (max 50)

    Returns ``list[dict]`` on success, or an error envelope on total upstream
    failure (``{"error": {"code": "ALL_BATCHES_FAILED", ...}}``).
    """
    exchange = _sanitize_us_exchange(exchange, "NASDAQ")
    timeframe = sanitize_timeframe(timeframe, "5m")
    rating = max(-3, min(3, rating))
    limit = max(1, min(limit, 50))
    try:
        rows = fetch_trending_analysis(exchange, timeframe=timeframe, filter_type="rating", rating_filter=rating, limit=limit)
    except BatchExecutionError as e:
        return make_error(
            ErrorCode.ALL_BATCHES_FAILED, str(e),
            batches_attempted=e.batches_attempted,
            batches_failed=e.batches_failed,
            first_error=e.first_error,
        )
    return [{"symbol": r["symbol"], "changePercent": r["changePercent"], "indicators": dict(r["indicators"])} for r in rows]


# ── Asset analysis ─────────────────────────────────────────────────────────────

@mcp.tool()
def asset_analysis(symbol: str, exchange: str = "NASDAQ", timeframe: str = "15m") -> dict:
    """Get detailed analysis for a U.S. stock or ETF on the specified exchange and timeframe.

    Args:
        symbol: U.S. symbol like "AAPL", "MSFT", "SPY", or "GDX"
        exchange: Exchange — NASDAQ, NYSE, AMEX, NYSEARCA, or PCX
        timeframe: Time interval (5m, 15m, 1h, 4h, 1D, 1W, 1M)

    Returns:
        Detailed analysis with all indicators, metrics, and stock-specific scoring when applicable
    """
    exchange = _sanitize_us_exchange(exchange, "NASDAQ")
    timeframe = sanitize_timeframe(timeframe, "15m")
    return analyze_asset(symbol, exchange, timeframe)

# ── Candle pattern tools ───────────────────────────────────────────────────────

@mcp.tool()
def consecutive_candles_scan(
    exchange: str = "NASDAQ",
    timeframe: str = "15m",
    pattern_type: str = "bullish",
    candle_count: int = 3,
    min_growth: float = 2.0,
    limit: int = 20,
) -> dict:
    """Scan for assets with consecutive growing/shrinking candles pattern.

    Args:
        exchange: Exchange name (NASDAQ, NYSE, or AMEX)
        timeframe: Time interval (5m, 15m, 1h, 4h)
        pattern_type: "bullish" (growing candles) or "bearish" (shrinking candles)
        candle_count: Number of consecutive candles to check (2-5)
        min_growth: Minimum growth percentage for each candle
        limit: Maximum number of results to return
    """
    exchange = _sanitize_us_exchange(exchange, "NASDAQ")
    timeframe = sanitize_timeframe(timeframe, "15m")
    candle_count = max(2, min(5, candle_count))
    min_growth = max(0.5, min(20.0, min_growth))
    limit = max(1, min(50, limit))
    return scan_consecutive_candles(exchange, timeframe, pattern_type, candle_count, min_growth, limit)


@mcp.tool()
def advanced_candle_pattern(
    exchange: str = "NASDAQ",
    base_timeframe: str = "15m",
    pattern_length: int = 3,
    min_size_increase: float = 10.0,
    limit: int = 15,
) -> dict:
    """Advanced candle pattern analysis using multi-timeframe data.

    Args:
        exchange: Exchange name (NASDAQ, NYSE, or AMEX)
        base_timeframe: Base timeframe for analysis (5m, 15m, 1h, 4h)
        pattern_length: Number of consecutive periods to analyse (2-4)
        min_size_increase: Minimum percentage increase in candle size
        limit: Maximum number of results to return
    """
    exchange = _sanitize_us_exchange(exchange, "NASDAQ")
    base_timeframe = sanitize_timeframe(base_timeframe, "15m")
    pattern_length = max(2, min(4, pattern_length))
    min_size_increase = max(5.0, min(50.0, min_size_increase))
    limit = max(1, min(30, limit))

    symbols = load_symbols(exchange)
    if not symbols:
        return {"error": f"No symbols found for exchange: {exchange}", "exchange": exchange}
    symbols = symbols[: min(limit * 2, 100)]

    if TRADINGVIEW_SCREENER_AVAILABLE:
        try:
            results = fetch_multi_timeframe_patterns(exchange, symbols, base_timeframe, pattern_length, min_size_increase)
            return {
                "exchange": exchange,
                "base_timeframe": base_timeframe,
                "pattern_length": pattern_length,
                "min_size_increase": min_size_increase,
                "method": "multi-timeframe",
                "total_found": len(results),
                "data": results[:limit],
            }
        except Exception:
            pass  # Fall through to single-timeframe fallback

    return scan_advanced_candle_patterns_single_tf(exchange, symbols, base_timeframe, pattern_length, min_size_increase, limit)


# ── Volume scanner tools ───────────────────────────────────────────────────────

@mcp.tool()
def volume_breakout_scanner(
    exchange: str = "NASDAQ",
    timeframe: str = "15m",
    volume_multiplier: float = 2.0,
    price_change_min: float = 3.0,
    limit: int = 25,
) -> list[dict] | dict:
    """Detect assets with volume breakout + price breakout.

    Args:
        exchange: Exchange name like NASDAQ, NYSE, or AMEX
        timeframe: One of 5m, 15m, 1h, 4h, 1D, 1W, 1M
        volume_multiplier: How many times the volume should be above normal level (default 2.0)
        price_change_min: Minimum price change percentage (default 3.0)
        limit: Number of rows to return (max 50)

    Returns ``list[dict]`` on success, or an error envelope on total upstream
    failure (``{"error": {"code": "ALL_BATCHES_FAILED", ...}}``). The empty
    list now strictly means "no matches today"; rate-limit cliffs surface
    explicitly.
    """
    exchange = _sanitize_us_exchange(exchange, "NASDAQ")
    timeframe = sanitize_timeframe(timeframe, "15m")
    volume_multiplier = max(1.5, min(10.0, volume_multiplier))
    price_change_min = max(1.0, min(20.0, price_change_min))
    limit = max(1, min(limit, 50))
    try:
        return volume_breakout_scan(exchange, timeframe, volume_multiplier, price_change_min, limit)
    except BatchExecutionError as e:
        return make_error(
            ErrorCode.ALL_BATCHES_FAILED, str(e),
            batches_attempted=e.batches_attempted,
            batches_failed=e.batches_failed,
            first_error=e.first_error,
        )


@mcp.tool()
def volume_confirmation_analysis(symbol: str, exchange: str = "NASDAQ", timeframe: str = "15m") -> dict:
    """Detailed volume confirmation analysis for a specific asset.

    Args:
        symbol: Asset symbol (e.g., AAPL, NVDA, SPY)
        exchange: Exchange name
        timeframe: Time frame for analysis
    """
    exchange = _sanitize_us_exchange(exchange, "NASDAQ")
    timeframe = sanitize_timeframe(timeframe, "15m")
    return volume_confirmation_analyze(symbol, exchange, timeframe)


@mcp.tool()
def smart_volume_scanner(
    exchange: str = "NASDAQ",
    min_volume_ratio: float = 2.0,
    min_price_change: float = 2.0,
    rsi_range: str = "any",
    limit: int = 20,
) -> list[dict] | dict:
    """Smart volume + technical analysis combination scanner.

    Args:
        exchange: Exchange name
        min_volume_ratio: Minimum volume multiplier (default 2.0)
        min_price_change: Minimum price change percentage (default 2.0)
        rsi_range: "oversold" (<30), "overbought" (>70), "neutral" (30-70), "any"
        limit: Number of results (max 30)

    Returns ``list[dict]`` on success, or an error envelope on total upstream
    failure (``{"error": {"code": "ALL_BATCHES_FAILED", ...}}``) — inherited
    from the inner ``volume_breakout_scan`` call.
    """
    exchange = _sanitize_us_exchange(exchange, "NASDAQ")
    min_volume_ratio = max(1.2, min(10.0, min_volume_ratio))
    min_price_change = max(0.5, min(20.0, min_price_change))
    limit = max(1, min(limit, 30))
    try:
        return smart_volume_scan(exchange, min_volume_ratio, min_price_change, rsi_range, limit)
    except BatchExecutionError as e:
        return make_error(
            ErrorCode.ALL_BATCHES_FAILED, str(e),
            batches_attempted=e.batches_attempted,
            batches_failed=e.batches_failed,
            first_error=e.first_error,
        )


# ── Multi-agent analysis ───────────────────────────────────────────────────────

@mcp.tool()
def multi_agent_analysis(symbol: str, exchange: str = "NASDAQ", timeframe: str = "15m") -> dict:
    """Run a multi-agent debate (Technical, Sentiment, Risk) for a U.S. stock or ETF.

    Args:
        symbol: U.S. symbol like "AAPL", "NVDA", "SPY", or "GDX"
        exchange: Exchange — NASDAQ, NYSE, AMEX, NYSEARCA, or PCX
        timeframe: Time interval (5m, 15m, 1h, 4h, 1D, 1W)

    Returns:
        A structured debate between 3 AI agents culminating in a final trading decision.
    """
    exchange = _sanitize_us_exchange(exchange, "NASDAQ")
    timeframe = sanitize_timeframe(timeframe, "15m")
    full_symbol = normalize_tradingview_symbol(symbol, exchange)
    return run_multi_agent_analysis(full_symbol, exchange, timeframe)


@mcp.tool()
def us_sector_scan(sector: str = "", timeframe: str = "1D") -> dict:
    """Show US GICS sector heat via SPDR sector ETF proxies (XLK, XLF, XLE, ...).

    Args:
        sector: Sector name (technology, health_care, financials,
                consumer_discretionary, communication_services, industrials,
                consumer_staples, energy, utilities, real_estate, materials).
                Leave empty to rank all 11 sectors by today's change%.
        timeframe: One of 5m, 15m, 1h, 4h, 1D, 1W, 1M
    """
    timeframe = sanitize_timeframe(timeframe, "1D")
    return scan_us_sector(sector, timeframe)






# ── Multi-timeframe analysis ───────────────────────────────────────────────────

@mcp.tool()
def multi_timeframe_analysis(symbol: str, exchange: str = "NASDAQ") -> dict:
    """Multi-timeframe alignment analysis (Monthly → Weekly → Daily → 4H → 1H → 15m).

    Args:
        symbol: U.S. symbol like "AAPL", "NVDA", "SPY", or "GDX"
        exchange: Exchange — NASDAQ, NYSE, AMEX, NYSEARCA, or PCX
    """
    exchange = _sanitize_us_exchange(exchange, "NASDAQ")
    full_symbol = normalize_tradingview_symbol(symbol, exchange)
    return run_multi_timeframe_analysis(full_symbol, exchange)


# ── News tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def financial_news(symbol: str = None, category: str = "stocks", limit: int = 10) -> dict:
    """Real-time market news from RSS feeds for U.S. equities and ETFs.

    Args:
        symbol: Optional symbol filter ("AAPL", "NVDA", "SPY"). None = all news.
        category: Feed category ("stocks" or "all")
        limit: Max number of news items
    """
    return fetch_news_summary(symbol, category, limit)


# ── Backtest tools ─────────────────────────────────────────────────────────────

@mcp.tool()
def backtest_strategy(
    symbol: str,
    strategy: str,
    period: str = "1y",
    initial_capital: float = 10000.0,
    commission_pct: float = 0.1,
    slippage_pct: float = 0.05,
    interval: str = "1d",
    include_trade_log: bool = False,
    include_equity_curve: bool = False,
) -> dict:
    """Backtest a trading strategy on historical data with institutional-grade metrics.

    Args:
        symbol: Yahoo Finance symbol (AAPL, SPY, QQQ, ^GSPC)
        strategy: rsi | bollinger | macd | ema_cross | supertrend | donchian
                  | rsi_pullback | keltner_breakout | triple_ema
                  (rsi_pullback and triple_ema need period >= '1y' for SMA200 warmup)
        period: '1mo', '3mo', '6mo', '1y', '2y'
        initial_capital: Starting capital in USD (default $10,000)
        commission_pct: Per-trade commission % (default 0.1%)
        slippage_pct: Per-trade slippage % (default 0.05%)
        interval: '1d' (daily) or '1h' (hourly)
        include_trade_log: Include full per-trade log (default False)
        include_equity_curve: Include equity curve data points (default False)
    """
    return run_backtest(
        symbol, strategy, period, initial_capital,
        commission_pct, slippage_pct, interval,
        include_trade_log, include_equity_curve,
    )


@mcp.tool()
def compare_strategies(
    symbol: str,
    period: str = "1y",
    initial_capital: float = 10000.0,
    interval: str = "1d",
) -> dict:
    """Run all 9 strategies (RSI, Bollinger, MACD, EMA Cross, Supertrend, Donchian, RSI Pullback, Keltner Breakout, Triple EMA) and return a ranked leaderboard.

    Args:
        symbol: Yahoo Finance symbol (AAPL, SPY, QQQ, ^GSPC)
        period: '1mo', '3mo', '6mo', '1y', '2y'
                (period >= '1y' recommended so rsi_pullback and triple_ema can
                 complete SMA200 warmup; otherwise they contribute zero trades)
        initial_capital: Starting capital in USD (default $10,000)
        interval: '1d' (daily) or '1h' (hourly)
    """
    return _compare_strategies(symbol, period, initial_capital, interval=interval)


@mcp.tool()
def walk_forward_backtest_strategy(
    symbol: str,
    strategy: str,
    period: str = "2y",
    initial_capital: float = 10000.0,
    commission_pct: float = 0.1,
    slippage_pct: float = 0.05,
    n_splits: int = 3,
    train_ratio: float = 0.7,
    interval: str = "1d",
) -> dict:
    """Walk-forward backtest to detect overfitting — validates strategy on unseen data.

    Args:
        symbol: Yahoo Finance symbol (AAPL, SPY, QQQ, ^GSPC)
        strategy: rsi | bollinger | macd | ema_cross | supertrend | donchian
                  | keltner_breakout
                  (rsi_pullback and triple_ema not supported here — SMA200 warmup
                   exceeds typical fold size; use run_backtest with period='2y')
        period: '1mo', '3mo', '6mo', '1y', '2y' (recommend '2y')
        initial_capital: Starting capital per fold in USD (default $10,000)
        commission_pct: Per-trade commission % (default 0.1%)
        slippage_pct: Per-trade slippage % (default 0.05%)
        n_splits: Number of walk-forward folds (default 3, max 10)
        train_ratio: Fraction of each fold used for training (default 0.7)
        interval: '1d' (daily) or '1h' (hourly)
    """
    return walk_forward_backtest(
        symbol, strategy, period, initial_capital,
        commission_pct, slippage_pct, n_splits, train_ratio, interval,
    )


# ── Yahoo Finance tools ────────────────────────────────────────────────────────

@mcp.tool()
def yahoo_price(symbol: str) -> dict:
    """Real-time price quote from Yahoo Finance for any stock, ETF, or index.

    Args:
        symbol: Yahoo Finance symbol — e.g. AAPL, SPY, QQQ, ^GSPC, EURUSD=X
    """
    return get_price(normalize_yahoo_symbol(symbol))


@mcp.tool()
def market_snapshot() -> dict:
    """Global market overview: major indices, FX rates, and key ETFs.
    Powered by Yahoo Finance.
    """
    return get_market_snapshot()


@mcp.tool()
def stock_extended_hours(symbol: str) -> dict:
    """Real-time pre-market and after-hours prices for a US stock symbol.

    Use this when the user asks about a stock outside the regular 9:30am-4pm
    ET session — earnings reactions, overnight news, "what is X doing in
    after-hours?", "how did Y open in pre-market?". Returns the most recent
    valid print from each session window (pre-market, regular, post-market)
    along with computed % changes vs. the previous close and the regular
    close, respectively.

    During the regular session, post_market will be null (no data yet).
    On weekends/holidays, returns whatever's most recent in each window.

    Args:
        symbol: US stock symbol — AAPL, NVDA, TSLA, SPY, ^GSPC, etc.

    Returns:
        - pre_market: {price, as_of_utc, change_vs_previous_close_pct} or null
        - regular: {price, as_of_utc, change_pct} (consolidated tape close)
        - post_market: {price, as_of_utc, change_vs_regular_close_pct} or null
        - previous_close, currency, exchange, market_state for context
    """
    return get_extended_hours_price(symbol)


@mcp.tool()
def stock_options_chain(symbol: str, expiry: Optional[str] = None) -> dict:
    """Full options chain (calls + puts) for a US stock symbol and one expiry.

    Use this when the user asks "what's the options chain for X?", "show me
    AAPL puts expiring next Friday", or wants to inspect bid/ask/IV/volume on
    a specific strike. If no expiry is provided, returns the nearest expiry
    so Claude can quote it back and ask "want a different one?".

    Args:
        symbol: US stock symbol — AAPL, NVDA, TSLA, SPY, etc.
        expiry: Optional ISO date (YYYY-MM-DD). Must match one of the
            `available_expiries` Yahoo returns; otherwise returns an error
            with the list of valid dates.

    Returns:
        - underlying_price, underlying_change_pct
        - requested_expiry, available_expiries (list of YYYY-MM-DD)
        - call_count, put_count
        - calls: list of {strike, last_price, bid, ask, volume,
          open_interest, implied_volatility, in_the_money, expiration}
        - puts: same shape as calls
    """
    return get_options_chain(symbol, expiry)


@mcp.tool()
def stock_options_unusual_activity(
    symbol: str,
    top_n: int = 10,
    min_volume: int = 100,
    expiries: int = 4,
) -> dict:
    """Top strikes by volume / open-interest ratio — institutional positioning signal.

    Use this when the user asks "any unusual options activity on X?", "where
    is the smart money positioned on NVDA before earnings?", or wants a
    V/OI screener for a ticker. A V/OI ratio > 1 means today's volume already
    exceeds standing open interest, which classically flags fresh institutional
    positioning on a specific strike in a specific direction (call vs put).

    Scans the soonest few expirations, filters out illiquid strikes (under
    `min_volume`), and returns the top-N sorted by V/OI descending. Also
    returns aggregate call vs put volume so Claude can comment on the
    overall directional bias.

    Args:
        symbol: US stock symbol — AAPL, NVDA, TSLA, SPY, META, etc.
        top_n: How many strikes to return. Default 10.
        min_volume: Filter floor for today's volume — prevents noise from
            illiquid strikes with high V/OI ratios. Default 100.
        expiries: Number of soonest expirations to scan. Default 4
            (typically covers ~1 month of weeklies + monthlies).

    Returns:
        - underlying_price
        - expiries_scanned (list of YYYY-MM-DD)
        - total_call_volume, total_put_volume, put_call_volume_ratio
        - unusual: list of top-N contracts sorted by V/OI desc, each with
          {strike, side (call|put), expiration, volume, open_interest,
          v_oi_ratio, last_price, implied_volatility, in_the_money,
          strike_vs_spot_pct (moneyness)}
    """
    return get_unusual_options_activity(symbol, top_n, min_volume, expiries)


# ── REST routes ────────────────────────────────────────────────────────────────

@mcp.custom_route("/api/assets/{symbol}/analysis", methods=["GET"])
async def rest_asset_analysis(request):
    return await _resolve_asset_route(request, "15m", asset_analysis)


@mcp.custom_route("/api/assets/{symbol}/multi-agent-analysis", methods=["GET"])
async def rest_multi_agent_analysis(request):
    return await _resolve_asset_route(request, "15m", multi_agent_analysis)


@mcp.custom_route("/api/assets/{symbol}/multi-timeframe-analysis", methods=["GET"])
async def rest_multi_timeframe_analysis(request):
    return await _resolve_asset_route(request, "1D", multi_timeframe_analysis, include_timeframe=False)


@mcp.custom_route("/api/assets/{symbol}/volume-confirmation", methods=["GET"])
async def rest_volume_confirmation(request):
    return await _resolve_asset_route(request, "15m", volume_confirmation_analysis)


@mcp.custom_route("/api/markets/{exchange}/gainers", methods=["GET"])
async def rest_top_gainers(request):
    return await _run_rest_handler(
        request,
        top_gainers,
        specs={"timeframe": (str, "15m"), "limit": (int, 25)},
        path_params={"exchange": request.path_params["exchange"]},
    )


@mcp.custom_route("/api/markets/{exchange}/losers", methods=["GET"])
async def rest_top_losers(request):
    return await _run_rest_handler(
        request,
        top_losers,
        specs={"timeframe": (str, "15m"), "limit": (int, 25)},
        path_params={"exchange": request.path_params["exchange"]},
    )


@mcp.custom_route("/api/markets/{exchange}/bollinger-scan", methods=["GET"])
async def rest_bollinger_scan(request):
    return await _run_rest_handler(
        request,
        bollinger_scan,
        specs={"timeframe": (str, "4h"), "bbw_threshold": (float, 0.04), "limit": (int, 50)},
        path_params={"exchange": request.path_params["exchange"]},
    )


@mcp.custom_route("/api/markets/{exchange}/rating-filter", methods=["GET"])
async def rest_rating_filter(request):
    return await _run_rest_handler(
        request,
        rating_filter,
        specs={"timeframe": (str, "5m"), "rating": (int, 2), "limit": (int, 25)},
        path_params={"exchange": request.path_params["exchange"]},
    )


@mcp.custom_route("/api/markets/{exchange}/consecutive-candles", methods=["GET"])
async def rest_consecutive_candles(request):
    return await _run_rest_handler(
        request,
        consecutive_candles_scan,
        specs={
            "timeframe": (str, "15m"),
            "pattern_type": (str, "bullish"),
            "candle_count": (int, 3),
            "min_growth": (float, 2.0),
            "limit": (int, 20),
        },
        path_params={"exchange": request.path_params["exchange"]},
    )


@mcp.custom_route("/api/markets/{exchange}/advanced-candle-pattern", methods=["GET"])
async def rest_advanced_candle_pattern(request):
    return await _run_rest_handler(
        request,
        advanced_candle_pattern,
        specs={
            "base_timeframe": (str, "15m"),
            "pattern_length": (int, 3),
            "min_size_increase": (float, 10.0),
            "limit": (int, 15),
        },
        path_params={"exchange": request.path_params["exchange"]},
    )


@mcp.custom_route("/api/markets/{exchange}/volume-breakouts", methods=["GET"])
async def rest_volume_breakouts(request):
    return await _run_rest_handler(
        request,
        volume_breakout_scanner,
        specs={
            "timeframe": (str, "15m"),
            "volume_multiplier": (float, 2.0),
            "price_change_min": (float, 3.0),
            "limit": (int, 25),
        },
        path_params={"exchange": request.path_params["exchange"]},
    )


@mcp.custom_route("/api/markets/{exchange}/smart-volume", methods=["GET"])
async def rest_smart_volume(request):
    return await _run_rest_handler(
        request,
        smart_volume_scanner,
        specs={
            "min_volume_ratio": (float, 2.0),
            "min_price_change": (float, 2.0),
            "rsi_range": (str, "any"),
            "limit": (int, 20),
        },
        path_params={"exchange": request.path_params["exchange"]},
    )


@mcp.custom_route("/api/markets/us/sectors", methods=["GET"])
async def rest_us_sectors(request):
    return await _run_rest_handler(
        request,
        us_sector_scan,
        specs={"sector": (str, ""), "timeframe": (str, "1D")},
    )


@mcp.custom_route("/api/news", methods=["GET"])
async def rest_financial_news(request):
    return await _run_rest_handler(
        request,
        financial_news,
        specs={"symbol": (str, None), "category": (str, "stocks"), "limit": (int, 10)},
    )


@mcp.custom_route("/api/yahoo/price/{symbol}", methods=["GET"])
async def rest_yahoo_price(request):
    return await _run_rest_handler(
        request,
        yahoo_price,
        path_params={"symbol": request.path_params["symbol"]},
    )


@mcp.custom_route("/api/yahoo/market-snapshot", methods=["GET"])
async def rest_market_snapshot(request):
    return await _run_rest_handler(request, market_snapshot)


@mcp.custom_route("/api/stocks/{symbol}/extended-hours", methods=["GET"])
async def rest_stock_extended_hours(request):
    return await _run_rest_handler(
        request,
        stock_extended_hours,
        path_params={"symbol": request.path_params["symbol"]},
    )


@mcp.custom_route("/api/stocks/{symbol}/options-chain", methods=["GET"])
async def rest_stock_options_chain(request):
    return await _run_rest_handler(
        request,
        stock_options_chain,
        specs={"expiry": (str, None)},
        path_params={"symbol": request.path_params["symbol"]},
    )


@mcp.custom_route("/api/stocks/{symbol}/options-unusual-activity", methods=["GET"])
async def rest_stock_options_unusual_activity(request):
    return await _run_rest_handler(
        request,
        stock_options_unusual_activity,
        specs={"top_n": (int, 10), "min_volume": (int, 100), "expiries": (int, 4)},
        path_params={"symbol": request.path_params["symbol"]},
    )


@mcp.custom_route("/api/backtests/run", methods=["POST"])
async def rest_backtest_strategy(request):
    return await _run_rest_handler(
        request,
        backtest_strategy,
        specs={
            "symbol": (str, _REQUIRED),
            "strategy": (str, _REQUIRED),
            "period": (str, "1y"),
            "initial_capital": (float, 10000.0),
            "commission_pct": (float, 0.1),
            "slippage_pct": (float, 0.05),
            "interval": (str, "1d"),
            "include_trade_log": (bool, False),
            "include_equity_curve": (bool, False),
        },
    )


@mcp.custom_route("/api/backtests/compare", methods=["POST"])
async def rest_compare_strategies(request):
    return await _run_rest_handler(
        request,
        compare_strategies,
        specs={
            "symbol": (str, _REQUIRED),
            "period": (str, "1y"),
            "initial_capital": (float, 10000.0),
            "interval": (str, "1d"),
        },
    )


@mcp.custom_route("/api/backtests/walk-forward", methods=["POST"])
async def rest_walk_forward_backtest(request):
    return await _run_rest_handler(
        request,
        walk_forward_backtest_strategy,
        specs={
            "symbol": (str, _REQUIRED),
            "strategy": (str, _REQUIRED),
            "period": (str, "2y"),
            "initial_capital": (float, 10000.0),
            "commission_pct": (float, 0.1),
            "slippage_pct": (float, 0.05),
            "n_splits": (int, 3),
            "train_ratio": (float, 0.7),
            "interval": (str, "1d"),
        },
    )


# ── Resource ───────────────────────────────────────────────────────────────────

@mcp.resource("exchanges://list")
def exchanges_list() -> str:
    """List the U.S. exchanges exposed by this server."""
    return "Available exchanges: AMEX, NASDAQ, NYSE"


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="TradingView Screener MCP server")
    parser.add_argument(
        "transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        nargs="?",
        help="Transport (default stdio)",
    )
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()

    if os.environ.get("DEBUG_MCP"):
        import sys
        print(f"[DEBUG_MCP] pkg cwd={os.getcwd()} argv={sys.argv} file={__file__}", file=sys.stderr, flush=True)

    if args.transport == "stdio":
        mcp.run()
    else:
        try:
            mcp.settings.host = args.host
            mcp.settings.port = args.port
        except Exception:
            pass
        mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
