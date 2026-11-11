"""Sessão do painel: login Google, cookie com token aleatório, hash no disco, CSRF por sessão.

Negado por padrão: sem sessão válida, nada do painel, da API ou da tela remota responde. Só os
e-mails de `ADMIN_EMAILS` (verificados pelo Google) criam sessão.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, WebSocket, status

from app.config import Settings, read_secrets
from app.oidc import OidcClient, OidcError
from app.state import write_json_atomic

logger = logging.getLogger(__name__)

SESSION_COOKIE = "lk_session"
FLOW_COOKIE = "lk_flow"
SESSION_TTL_S = 30 * 86_400
FLOW_TTL_S = 600
MAX_SESSIONS = 10
MAX_FLOWS = 32
# Para onde o login volta além de "/": só o link de pedido de acesso (allowlist, sem redirect
# aberto). O link chega pelo WhatsApp e o dono pode estar sem sessão no aparelho.
RETURN_PATH = re.compile(r"^/acesso/[A-Za-z0-9_-]{20,64}$")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@dataclass(frozen=True, slots=True)
class Session:
    email: str
    csrf: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class _Flow:
    binding_hash: str
    nonce: str
    verifier: str
    expires_at: float
    return_to: str = "/"


class LoginError(Exception):
    def __init__(self, code: str, status_code: int = status.HTTP_400_BAD_REQUEST) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class AuthService:
    def __init__(self, settings: Settings, oidc: OidcClient) -> None:
        self._settings = settings
        self._oidc = oidc
        self._path = settings.data_dir / "sessions.json"
        self._sessions: dict[str, Session] = {}  # sha256(token) -> sessão
        self._flows: OrderedDict[str, _Flow] = OrderedDict()  # sha256(state) -> fluxo
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        raw = await asyncio.to_thread(_read_json, self._path)
        now = time.time()
        for key, value in (raw or {}).items():
            try:
                sess = Session(str(value["email"]), str(value["csrf"]), float(value["expires_at"]))
            except (KeyError, TypeError, ValueError):
                continue
            if sess.expires_at > now and sess.email in self._settings.allowed_email_set:
                self._sessions[key] = sess

    # --- fluxo OIDC ---------------------------------------------------------------------------

    def start_login(self, return_to: str | None = None) -> tuple[str, str]:
        """Devolve (URL do Google, valor do cookie que amarra o fluxo a este navegador). Um
        `return_to` fora da allowlist vira "/"."""
        if not self._settings.google_client_id:
            raise LoginError("google_nao_configurado", status.HTTP_503_SERVICE_UNAVAILABLE)
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        verifier, binding = secrets.token_urlsafe(64), secrets.token_urlsafe(32)
        now = time.time()
        for key in [k for k, f in self._flows.items() if f.expires_at <= now]:
            del self._flows[key]
        while len(self._flows) >= MAX_FLOWS:
            self._flows.popitem(last=False)
        target = return_to if return_to and RETURN_PATH.fullmatch(return_to) else "/"
        self._flows[_sha(state)] = _Flow(
            _sha(binding), nonce, verifier, now + FLOW_TTL_S, return_to=target
        )
        url = self._oidc.authorize_url(
            state=state, nonce=nonce, code_challenge=_challenge(verifier)
        )
        return url, binding

    async def finish_login(
        self, *, state: str | None, code: str | None, error: str | None, binding: str | None
    ) -> tuple[str, Session, str]:
        """Devolve (token do cookie, sessão, caminho de volta). Levanta LoginError com o código
        para a tela."""
        if not state or len(state) > 128:
            raise LoginError("estado_invalido")
        # O state é gasto antes de qualquer chamada de rede: replay nunca chega ao Google.
        flow = self._flows.pop(_sha(state), None)
        if flow is None or flow.expires_at <= time.time():
            raise LoginError("estado_invalido")
        if not binding or not hmac.compare_digest(_sha(binding), flow.binding_hash):
            raise LoginError("estado_invalido")
        if error:
            raise LoginError("cancelado")
        if not code or len(code) > 2048:
            raise LoginError("login_invalido")

        client_secret = (
            await asyncio.to_thread(read_secrets, self._settings.secrets_file)
        ).google_client_secret
        if not client_secret:
            raise LoginError("google_nao_configurado", status.HTTP_503_SERVICE_UNAVAILABLE)
        try:
            id_token = await self._oidc.exchange_code(code, flow.verifier, client_secret)
            identity = await self._oidc.verify_id_token(id_token)
        except OidcError as exc:
            logger.info("google login refused: %s", exc.code)
            code_status = (
                status.HTTP_503_SERVICE_UNAVAILABLE
                if exc.code == "google_indisponivel"
                else status.HTTP_403_FORBIDDEN
            )
            raise LoginError(exc.code, code_status) from exc
        if identity.nonce is None or not hmac.compare_digest(identity.nonce, flow.nonce):
            raise LoginError("login_invalido", status.HTTP_403_FORBIDDEN)
        if identity.email not in self._settings.allowed_email_set:
            logger.warning("google login denied for a non-allowed account")
            raise LoginError("conta_nao_autorizada", status.HTTP_403_FORBIDDEN)

        token = secrets.token_urlsafe(32)
        sess = Session(identity.email, secrets.token_urlsafe(24), time.time() + SESSION_TTL_S)
        async with self._lock:
            now = time.time()
            self._sessions = {k: s for k, s in self._sessions.items() if s.expires_at > now}
            while len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions, key=lambda k: self._sessions[k].expires_at)
                del self._sessions[oldest]
            self._sessions[_sha(token)] = sess
            await self._save()
        logger.info("panel login ok")
        return token, sess, flow.return_to

    # --- sessão --------------------------------------------------------------------------------

    def session_for(self, token: str | None) -> Session | None:
        if not token or len(token) > 128:
            return None
        sess = self._sessions.get(_sha(token))
        if sess is None or sess.expires_at <= time.time():
            return None
        if sess.email not in self._settings.allowed_email_set:
            return None  # e-mail saiu da allowlist: sessão antiga deixa de valer
        return sess

    async def logout(self, token: str | None) -> None:
        if not token:
            return
        async with self._lock:
            if self._sessions.pop(_sha(token), None) is not None:
                await self._save()

    async def _save(self) -> None:
        payload: dict[str, Any] = {k: asdict(s) for k, s in self._sessions.items()}
        await asyncio.to_thread(write_json_atomic, self._path, payload)

    # --- checagens por requisição -----------------------------------------------------------

    def require(self, request: Request) -> Session:
        sess = self.session_for(request.cookies.get(SESSION_COOKIE))
        if sess is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="login")
        return sess

    def require_write(self, request: Request) -> Session:
        """POST: sessão + Origin do próprio painel + cabeçalho CSRF da sessão."""
        sess = self.require(request)
        if request.headers.get("origin") != self._settings.public_origin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="origin")
        submitted = request.headers.get("x-csrf-token", "")
        if not submitted or not hmac.compare_digest(submitted, sess.csrf):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="csrf")
        return sess

    def websocket_session(self, ws: WebSocket) -> Session | None:
        if ws.headers.get("origin") != self._settings.public_origin:
            return None
        return self.session_for(ws.cookies.get(SESSION_COOKIE))


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("sessions file unreadable: %s", type(exc).__name__)
        return None
    return data if isinstance(data, dict) else None
