"""Minimal OAuth server with an interactive authorize form.

This keeps the project on a shared-secret auth model while exposing an
authorization-code + PKCE flow that browser-based clients such as Postman can
complete. The user authenticates by entering the configured bearer token on the
authorize page, after which the server issues normal OAuth access/refresh
tokens for the MCP resource.
"""
from __future__ import annotations

import base64
import hashlib
import html
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

_AUTH_CODE_TTL_SECONDS = 600
_ACCESS_TOKEN_TTL_SECONDS = 3600
_REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600
_DEFAULT_SCOPE = "*"
_AUTHORIZE_PARAMS = (
    "response_type",
    "client_id",
    "redirect_uri",
    "state",
    "code_challenge",
    "code_challenge_method",
    "scope",
    "resource",
)


def _normalize_scopes(scope: str | None) -> list[str]:
    if scope is None:
        return [_DEFAULT_SCOPE]
    scopes = [part for part in scope.split(" ") if part]
    return scopes or [_DEFAULT_SCOPE]


def _now() -> int:
    return int(time.time())


def escape_html(value: str) -> str:
    return html.escape(value, quote=True)


def render_authorize_form(server_name: str, params: dict[str, str], error: str | None = None) -> str:
    hidden = "\n      ".join(
        f'<input type="hidden" name="{escape_html(key)}" value="{escape_html(value)}">'
        for key, value in params.items()
    )
    error_block = f'<p style="color: #b42318;">{escape_html(error)}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html>
  <head>
    <title>Authorize {escape_html(server_name)}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
  </head>
  <body style="font-family: sans-serif; max-width: 420px; margin: 80px auto; padding: 0 16px;">
    <h2>Authorize {escape_html(server_name)}</h2>
    <p>Enter your configured bearer token to authorize this client.</p>
    {error_block}
    <form method="POST" action="/authorize">
      {hidden}
      <label for="token">Bearer token</label><br>
      <input type="password" id="token" name="token" style="width: 100%; padding: 8px; margin: 8px 0; box-sizing: border-box;" autofocus required>
      <button type="submit" style="padding: 8px 16px;">Authorize</button>
    </form>
  </body>
</html>"""


def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    sha256 = hashlib.sha256(code_verifier.encode()).digest()
    hashed = base64.urlsafe_b64encode(sha256).decode().rstrip("=")
    return secrets.compare_digest(hashed, code_challenge)


@dataclass
class OAuthError(Exception):
    error: str
    description: str
    status_code: int = 400


class SharedSecretOAuthServer:
    def __init__(self, shared_token: str, server_name: str) -> None:
        self.shared_token = shared_token
        self.server_name = server_name
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}

    def register_client(self, redirect_uris: list[str], client_name: str | None = None) -> OAuthClientInformationFull:
        if not redirect_uris:
            raise OAuthError("invalid_client_metadata", "redirect_uris is required")

        client_id = str(uuid4())
        client = OAuthClientInformationFull(
            client_id=client_id,
            redirect_uris=redirect_uris,
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=_DEFAULT_SCOPE,
            client_name=client_name,
            client_id_issued_at=_now(),
            client_secret=None,
            client_secret_expires_at=None,
        )
        self._clients[client_id] = client
        return client

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    def extract_authorize_params(self, source: dict[str, Any]) -> dict[str, str]:
        params: dict[str, str] = {}
        for key in _AUTHORIZE_PARAMS:
            value = source.get(key)
            if isinstance(value, str):
                params[key] = value
        return params

    def validate_authorize_request(self, params: dict[str, str]) -> OAuthClientInformationFull:
        client_id = params.get("client_id", "")
        client = self.get_client(client_id)
        if not client:
            raise OAuthError("invalid_request", f"Client ID '{client_id}' not found")

        redirect_uri = params.get("redirect_uri")
        if not redirect_uri or redirect_uri not in {str(uri) for uri in client.redirect_uris}:
            raise OAuthError("invalid_request", "Invalid redirect_uri for this client")

        if params.get("response_type") != "code":
            raise OAuthError("unsupported_response_type", "Only authorization code flow is supported")
        if params.get("code_challenge_method") != "S256":
            raise OAuthError("invalid_request", "Only S256 PKCE is supported")
        if not params.get("code_challenge"):
            raise OAuthError("invalid_request", "Missing code_challenge")

        return client

    def create_authorization_redirect(self, params: dict[str, str]) -> str:
        client = self.validate_authorize_request(params)
        code = secrets.token_urlsafe(32)
        redirect_uri = params["redirect_uri"]
        scopes = _normalize_scopes(params.get("scope"))
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=scopes,
            expires_at=time.time() + _AUTH_CODE_TTL_SECONDS,
            client_id=client.client_id,
            code_challenge=params["code_challenge"],
            redirect_uri=redirect_uri,
            redirect_uri_provided_explicitly=True,
            resource=params.get("resource"),
        )

        query = {"code": code}
        state = params.get("state")
        if state:
            query["state"] = state
        separator = "&" if "?" in redirect_uri else "?"
        return f"{redirect_uri}{separator}{urlencode(query)}"

    def exchange_code(
        self,
        *,
        client_id: str,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> dict[str, Any]:
        client = self.get_client(client_id)
        if not client:
            raise OAuthError("invalid_client", "Invalid client_id")

        auth_code = self._auth_codes.pop(code, None)
        if not auth_code or auth_code.client_id != client_id:
            raise OAuthError("invalid_grant", "authorization code does not exist")
        if auth_code.expires_at < time.time():
            raise OAuthError("invalid_grant", "authorization code has expired")
        if str(auth_code.redirect_uri) != redirect_uri:
            raise OAuthError("invalid_request", "redirect_uri did not match the one used when creating auth code")
        if not verify_pkce(code_verifier, auth_code.code_challenge):
            raise OAuthError("invalid_grant", "incorrect code_verifier")

        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        now = _now()
        scopes = auth_code.scopes or [_DEFAULT_SCOPE]
        self._access_tokens[access_token] = AccessToken(
            token=access_token,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + _ACCESS_TOKEN_TTL_SECONDS,
            resource=auth_code.resource,
        )
        self._refresh_tokens[refresh_token] = RefreshToken(
            token=refresh_token,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + _REFRESH_TOKEN_TTL_SECONDS,
        )
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": _ACCESS_TOKEN_TTL_SECONDS,
            "refresh_token": refresh_token,
            "scope": " ".join(scopes),
        }

    def exchange_refresh_token(self, *, client_id: str, refresh_token: str) -> dict[str, Any]:
        client = self.get_client(client_id)
        if not client:
            raise OAuthError("invalid_client", "Invalid client_id")

        entry = self._refresh_tokens.get(refresh_token)
        if not entry or entry.client_id != client_id:
            raise OAuthError("invalid_grant", "refresh token does not exist")
        if entry.expires_at is not None and entry.expires_at < _now():
            self._refresh_tokens.pop(refresh_token, None)
            raise OAuthError("invalid_grant", "refresh token has expired")

        new_access_token = secrets.token_urlsafe(32)
        self._access_tokens[new_access_token] = AccessToken(
            token=new_access_token,
            client_id=client_id,
            scopes=entry.scopes,
            expires_at=_now() + _ACCESS_TOKEN_TTL_SECONDS,
        )
        return {
            "access_token": new_access_token,
            "token_type": "Bearer",
            "expires_in": _ACCESS_TOKEN_TTL_SECONDS,
            "scope": " ".join(entry.scopes),
        }

    async def verify_token(self, token: str) -> AccessToken | None:
        if secrets.compare_digest(token, self.shared_token):
            return AccessToken(token=token, client_id="shared-secret", scopes=[_DEFAULT_SCOPE])

        entry = self._access_tokens.get(token)
        if not entry:
            return None
        if entry.expires_at is not None and entry.expires_at < _now():
            self._access_tokens.pop(token, None)
            return None
        return entry

    def is_valid_bearer_token(self, token: str) -> bool:
        if secrets.compare_digest(token, self.shared_token):
            return True
        entry = self._access_tokens.get(token)
        if not entry:
            return False
        if entry.expires_at is not None and entry.expires_at < _now():
            self._access_tokens.pop(token, None)
            return False
        return True
