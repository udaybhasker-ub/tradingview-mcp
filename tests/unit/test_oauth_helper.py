from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from tradingview_mcp.core.auth import OAuthError, SharedSecretOAuthServer, verify_pkce


def test_pkce_verifier_matches_rfc_example_shape():
    # Deterministic smoke test around the hashing format we use later in token exchange.
    challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert verify_pkce("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk", challenge) is True


def test_oauth_helper_supports_register_authorize_exchange_and_refresh():
    auth = SharedSecretOAuthServer("topsecret", "TradingView MCP")
    client = auth.register_client(["https://oauth.pstmn.io/v1/callback"], "Postman")

    params = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": "https://oauth.pstmn.io/v1/callback",
        "code_challenge_method": "S256",
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "state": "abc123",
        "scope": "*",
    }
    redirect = auth.create_authorization_redirect(params)
    parsed = urlparse(redirect)
    query = parse_qs(parsed.query)
    code = query["code"][0]
    assert query["state"] == ["abc123"]

    token_payload = auth.exchange_code(
        client_id=client.client_id,
        code=code,
        redirect_uri="https://oauth.pstmn.io/v1/callback",
        code_verifier="dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    )
    assert token_payload["token_type"] == "Bearer"
    assert auth.is_valid_bearer_token(token_payload["access_token"]) is True

    refreshed = auth.exchange_refresh_token(
        client_id=client.client_id,
        refresh_token=token_payload["refresh_token"],
    )
    assert refreshed["token_type"] == "Bearer"
    assert auth.is_valid_bearer_token(refreshed["access_token"]) is True
    assert auth.is_valid_bearer_token("topsecret") is True


def test_exchange_rejects_wrong_pkce_verifier():
    auth = SharedSecretOAuthServer("topsecret", "TradingView MCP")
    client = auth.register_client(["https://oauth.pstmn.io/v1/callback"])
    params = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": "https://oauth.pstmn.io/v1/callback",
        "code_challenge_method": "S256",
        "code_challenge": "abc",
    }
    redirect = auth.create_authorization_redirect(params)
    code = parse_qs(urlparse(redirect).query)["code"][0]

    with pytest.raises(OAuthError) as excinfo:
        auth.exchange_code(
            client_id=client.client_id,
            code=code,
            redirect_uri="https://oauth.pstmn.io/v1/callback",
            code_verifier="wrong",
        )

    assert excinfo.value.error == "invalid_grant"
