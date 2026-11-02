"""Google OpenID Connect para o painel: URL de autorização, troca do código, checagem do id_token.

Adaptado de mucommerce/apps/api-commerce/app/customers/oidc.py: authorization code + PKCE (S256)
com nonce. A assinatura RS256 do id_token é conferida contra as chaves publicadas pelo Google, junto
com iss, aud, exp, iat e `email_verified`. O conjunto de chaves é buscado com timeout, guardado
pelo `max-age` que o Google manda (limitado) e rebuscado no máximo uma vez por minuto quando
aparece um kid desconhecido; se a renovação falhar, fica o último conjunto bom.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from app.config import GOOGLE_ISSUER, Settings

GOOGLE_ISSUERS = (GOOGLE_ISSUER, "accounts.google.com")
CLOCK_SKEW_SECONDS = 60
JWKS_MIN_TTL = 300
JWKS_MAX_TTL = 86_400
JWKS_REFETCH_EVERY = 60
_MAX_AGE = re.compile(r"max-age=(\d+)")


class OidcError(Exception):
    """O login não completou. `code` pode ir para a tela (`/?erro=<code>`)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


@dataclass(frozen=True, slots=True)
class GoogleIdentity:
    subject: str
    email: str
    nonce: str | None


class OidcClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._keys: jwt.PyJWKSet | None = None
        self._expires_at = 0.0
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._settings.google_oidc_timeout_seconds, transport=self._transport
        )

    def authorize_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "client_id": self._settings.google_client_id,
                "redirect_uri": self._settings.redirect_uri,
                "response_type": "code",
                "scope": "openid email",
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        return f"{self._settings.google_oidc_authorize_url}?{query}"

    async def exchange_code(self, code: str, code_verifier: str, client_secret: str) -> str:
        """Troca o código pelo id_token. Sem retry: código é de uso único."""
        data = {
            "code": code,
            "client_id": self._settings.google_client_id,
            "client_secret": client_secret,
            "redirect_uri": self._settings.redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        }
        try:
            async with self._client() as client:
                response = await client.post(self._settings.google_oidc_token_url, data=data)
        except httpx.HTTPError as exc:
            raise OidcError("google_indisponivel") from exc
        if 400 <= response.status_code < 500:
            # Código vencido, reusado ou forjado. Nunca logar o corpo (ecoa a entrada).
            raise OidcError("login_invalido")
        if response.status_code != 200:
            raise OidcError("google_indisponivel")
        try:
            id_token = response.json()["id_token"]
        except (ValueError, KeyError, TypeError) as exc:
            raise OidcError("google_indisponivel", "token response without id_token") from exc
        if not isinstance(id_token, str):
            raise OidcError("google_indisponivel", "id_token is not a string")
        return id_token

    async def _fetch_keys(self) -> None:
        async with self._client() as client:
            response = await client.get(self._settings.google_oidc_jwks_url)
        response.raise_for_status()
        keys = jwt.PyJWKSet.from_dict(response.json())
        match = _MAX_AGE.search(response.headers.get("cache-control", ""))
        ttl = int(match.group(1)) if match else JWKS_MIN_TTL
        now = time.monotonic()
        self._keys = keys
        self._fetched_at = now
        self._expires_at = now + min(max(ttl, JWKS_MIN_TTL), JWKS_MAX_TTL)

    async def _signing_key(self, kid: str) -> jwt.PyJWK:
        async with self._lock:
            now = time.monotonic()
            known = self._keys is not None and kid in {k.key_id for k in self._keys.keys}
            stale = now >= self._expires_at
            may_refetch = now - self._fetched_at >= JWKS_REFETCH_EVERY or self._keys is None
            if stale or (not known and may_refetch):
                try:
                    await self._fetch_keys()
                except (httpx.HTTPError, ValueError, jwt.PyJWKSetError) as exc:
                    if self._keys is None:
                        raise OidcError("google_indisponivel") from exc
            if self._keys is None:
                raise OidcError("google_indisponivel")
            try:
                return self._keys[kid]
            except KeyError as exc:
                raise OidcError("login_invalido", "unknown key id") from exc

    async def verify_id_token(self, id_token: str) -> GoogleIdentity:
        """Assinatura, emissor, audiência e horários. O nonce é conferido por quem chama."""
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise OidcError("login_invalido", "malformed id_token") from exc
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise OidcError("login_invalido", "unexpected id_token header")
        key = await self._signing_key(header["kid"])
        try:
            claims: dict[str, Any] = jwt.decode(
                id_token,
                key=key.key,
                algorithms=["RS256"],
                audience=self._settings.google_client_id,
                issuer=GOOGLE_ISSUERS,
                leeway=CLOCK_SKEW_SECONDS,
                options={"require": ["iss", "aud", "sub", "exp", "iat"]},
            )
        except jwt.PyJWTError as exc:
            raise OidcError("login_invalido", type(exc).__name__) from exc

        email = claims.get("email")
        verified = claims.get("email_verified") in (True, "true")
        if not isinstance(email, str) or not email or not verified:
            raise OidcError("email_nao_verificado")
        nonce = claims.get("nonce")
        return GoogleIdentity(
            subject=str(claims["sub"]),
            email=email.strip().lower(),
            nonce=nonce if isinstance(nonce, str) else None,
        )
