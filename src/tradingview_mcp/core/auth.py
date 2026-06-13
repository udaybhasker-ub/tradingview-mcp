"""Minimal OAuth 2.1 authorization server provider.

Implements just enough of the OAuth dance (dynamic client registration,
authorization code + PKCE, token exchange) for Claude.ai's remote MCP
connector flow, which requires an OAuth-shaped handshake even when access
is gated by a single shared secret.

There is no real user login: every client that registers is auto-approved,
and the authorization code always exchanges for the same pre-shared bearer
token (MCP_AUTH_TOKEN). Requests to /mcp must present that token.
"""
from __future__ import annotations

import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    OAuthClientInformationFull,
    OAuthToken,
    RefreshToken,
    construct_redirect_uri,
)

_AUTH_CODE_TTL_SECONDS = 600


class SharedSecretOAuthProvider(OAuthAuthorizationServerProvider):
    def __init__(self, shared_token: str) -> None:
        self._shared_token = shared_token
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        code = secrets.token_urlsafe(32)
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + _AUTH_CODE_TTL_SECONDS,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return self._auth_codes.get(authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._auth_codes.pop(authorization_code.code, None)
        return OAuthToken(
            access_token=self._shared_token,
            token_type="Bearer",
            scope=" ".join(authorization_code.scopes),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        raise NotImplementedError("refresh tokens are not issued; clients must re-authorize")

    async def load_access_token(self, token: str) -> AccessToken | None:
        if secrets.compare_digest(token, self._shared_token):
            return AccessToken(token=token, client_id="claude-ai", scopes=["*"])
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        pass
