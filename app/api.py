"""Rotas do painel. Só /healthz e a tela de login ficam abertos; o resto exige a sessão Google."""

from __future__ import annotations

import asyncio
import html
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, status
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, Field, field_validator

from app.access import ID_PATTERN, TOKEN_PATTERN, AccessError, AccessService
from app.access import iso as iso_z
from app.auth import (
    FLOW_COOKIE,
    FLOW_TTL_S,
    SESSION_COOKIE,
    SESSION_TTL_S,
    AuthService,
    LoginError,
    Session,
)
from app.config import Settings
from app.slots import ACCOUNT_ID_PATTERN, MAX_ACCOUNTS, SlotError, SlotManager, SlotRuntime
from app.vnc import bridge

WEB_DIR = Path(__file__).parent / "web"
STATIC_FILES = {
    "app.js": "text/javascript",
    "styles.css": "text/css",
    "remota.js": "text/javascript",
    "remota.css": "text/css",
    "acesso.js": "text/javascript",
}
SCREENSHOT_MIN_INTERVAL_S = 2.0
# Tela remota: o visor próprio (/remota, sobre o núcleo do noVNC) é o padrão; a UI do noVNC fica
# de reserva em /vnc/vnc.html.
REMOTE_URL = "/remota"
NOVNC_QUERY = urlencode({"path": "vnc/ws", "autoconnect": "1", "resize": "scale", "reconnect": "1"})

PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "Referrer-Policy": "no-referrer",
}

# Código de recusa do domínio (SlotError.code) -> status HTTP. Fora da tabela: 409.
SLOT_ERROR_STATUS = {
    "sem_chave": status.HTTP_503_SERVICE_UNAVAILABLE,
    "conta_nao_encontrada": status.HTTP_404_NOT_FOUND,
}
# Idem para o acesso à tela remota de outra instância (AccessError.code).
ACCESS_ERROR_STATUS = {
    "nao_admin": status.HTTP_403_FORBIDDEN,
    "outra_conta": status.HTTP_403_FORBIDDEN,
    "acesso_negado": status.HTTP_403_FORBIDDEN,
    "slot_nao_encontrado": status.HTTP_404_NOT_FOUND,
    "pedido_nao_encontrado": status.HTTP_404_NOT_FOUND,
    "acesso_nao_encontrado": status.HTTP_404_NOT_FOUND,
    "prazo_invalido": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "pedidos_demais": status.HTTP_429_TOO_MANY_REQUESTS,
    "acessos_demais": status.HTTP_429_TOO_MANY_REQUESTS,
}

LOGIN_ERRORS = {
    "conta_nao_autorizada": "Esta conta Google não tem acesso a este painel.",
    "email_nao_verificado": "O Google não confirmou o e-mail desta conta.",
    "cancelado": "Login cancelado no Google.",
    "estado_invalido": "O login demorou demais ou veio de outra aba. Tente de novo.",
    "login_invalido": "O Google recusou o login. Tente de novo.",
    "google_indisponivel": "O Google não respondeu. Tente de novo em instantes.",
    "google_nao_configurado": "Login Google ainda não configurado no servidor.",
}


@dataclass
class Services:
    auth: AuthService
    slots: SlotManager
    access: AccessService


class PowerIn(BaseModel):
    on: bool


class AccessRequestIn(BaseModel):
    slot: int = Field(ge=1, le=8)


class ApproveIn(BaseModel):
    hours: Literal[1, 12, 168]


class AccountIn(BaseModel):
    # Nome de caçador (ou e-mail da conta) e senha do jogo. A senha nunca volta em resposta.
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=128, repr=False)

    @field_validator("username")
    @classmethod
    def _clean_username(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3 or any(c.isspace() or not c.isprintable() for c in value):
            raise ValueError("nome de caçador inválido")
        return value

    @field_validator("password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        if any(c in value for c in ("\r", "\n", "\x00")):
            raise ValueError("senha com caractere inválido")
        return value


def services(request: Request) -> Services:
    svc: Services = request.app.state.services
    return svc


Svc = Annotated[Services, Depends(services)]
AccountId = Annotated[str, PathParam(pattern=ACCOUNT_ID_PATTERN)]
AccessId = Annotated[str, PathParam(pattern=ID_PATTERN)]
LinkToken = Annotated[str, PathParam(pattern=TOKEN_PATTERN)]
SlotQuery = Annotated[int | None, Query(ge=1, le=8)]


def read_session(request: Request, svc: Svc) -> Session:
    return svc.auth.require(request)


def write_session(request: Request, svc: Svc) -> Session:
    return svc.auth.require_write(request)


ReadSession = Annotated[Session, Depends(read_session)]
WriteSession = Annotated[Session, Depends(write_session)]


def _refused(exc: SlotError) -> HTTPException:
    return HTTPException(SLOT_ERROR_STATUS.get(exc.code, status.HTTP_409_CONFLICT), detail=exc.code)


def _access_refused(exc: AccessError) -> HTTPException:
    return HTTPException(
        ACCESS_ERROR_STATUS.get(exc.code, status.HTTP_409_CONFLICT), detail=exc.code
    )


def _runtime(svc: Services, sess: Session) -> SlotRuntime:
    """O slot vem do e-mail da sessão, nunca de um id do navegador."""
    runtime = svc.slots.for_email(sess.email)
    if runtime is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="sem_instancia")
    return runtime


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    def cookie_kwargs() -> dict[str, Any]:
        return {"secure": True, "httponly": True, "samesite": "lax"}

    # --- abertas ------------------------------------------------------------------------------

    @router.get("/healthz")
    async def healthz(svc: Svc) -> JSONResponse:
        ok = svc.slots.healthy()
        return JSONResponse(
            {"ok": ok, "slots": svc.slots.used},
            status_code=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @router.get("/", response_class=HTMLResponse)
    async def index(request: Request, svc: Svc) -> Response:
        page = (
            "index.html"
            if svc.auth.session_for(request.cookies.get(SESSION_COOKIE))
            else "login.html"
        )
        return FileResponse(WEB_DIR / page, media_type="text/html", headers=PAGE_HEADERS)

    @router.get("/static/{name}")
    async def static(name: str) -> Response:
        media_type = STATIC_FILES.get(name)
        if media_type is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        return FileResponse(
            WEB_DIR / name, media_type=media_type, headers={"Cache-Control": "no-cache"}
        )

    # --- login Google ---------------------------------------------------------------------------

    @router.get("/auth/login")
    async def login(
        svc: Svc, next_path: Annotated[str | None, Query(alias="next", max_length=128)] = None
    ) -> Response:
        try:
            url, binding = svc.auth.start_login(next_path)
        except LoginError as exc:
            return _error_page(exc)
        response = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            FLOW_COOKIE, binding, max_age=FLOW_TTL_S, path="/auth", **cookie_kwargs()
        )
        return response

    @router.get("/auth/callback")
    async def callback(
        request: Request,
        svc: Svc,
        state: Annotated[str | None, Query(max_length=256)] = None,
        code: Annotated[str | None, Query(max_length=4096)] = None,
        error: Annotated[str | None, Query(max_length=256)] = None,
    ) -> Response:
        try:
            token, _sess, return_to = await svc.auth.finish_login(
                state=state, code=code, error=error, binding=request.cookies.get(FLOW_COOKIE)
            )
        except LoginError as exc:
            refused = _error_page(exc)
            refused.delete_cookie(FLOW_COOKIE, path="/auth", **cookie_kwargs())
            return refused
        response = RedirectResponse(return_to, status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(FLOW_COOKIE, path="/auth", **cookie_kwargs())
        response.set_cookie(
            SESSION_COOKIE, token, max_age=SESSION_TTL_S, path="/", **cookie_kwargs()
        )
        return response

    @router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(request: Request, svc: Svc, _s: WriteSession) -> Response:
        await svc.auth.logout(request.cookies.get(SESSION_COOKIE))
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(SESSION_COOKIE, path="/", **cookie_kwargs())
        return response

    # --- API ------------------------------------------------------------------------------------

    @router.get("/api/status")
    async def get_status(svc: Svc, sess: ReadSession) -> JSONResponse:
        return await _status_response(svc, sess)

    @router.post("/api/accounts")
    async def save_account(body: AccountIn, svc: Svc, sess: WriteSession) -> JSONResponse:
        """Conta nova (ou senha nova de um caçador da lista). Sem instância, cria a instância."""
        try:
            await svc.slots.save_account(sess.email, body.username, body.password)
        except SlotError as exc:
            raise _refused(exc) from exc
        return await _status_response(svc, sess)

    @router.post("/api/accounts/{account_id}/activate")
    async def activate_account(account_id: AccountId, svc: Svc, sess: WriteSession) -> JSONResponse:
        try:
            await svc.slots.activate_account(sess.email, account_id)
        except SlotError as exc:
            raise _refused(exc) from exc
        except OSError as exc:
            # Perfil não saiu do disco: a conta não troca (o navegador reabre na anterior).
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="troca_falhou") from exc
        return await _status_response(svc, sess)

    @router.delete("/api/accounts/{account_id}")
    async def remove_account(account_id: AccountId, svc: Svc, sess: WriteSession) -> JSONResponse:
        try:
            await svc.slots.remove_account(sess.email, account_id)
        except SlotError as exc:
            raise _refused(exc) from exc
        return await _status_response(svc, sess)

    @router.delete("/api/slot")
    async def delete_slot(svc: Svc, sess: WriteSession) -> JSONResponse:
        if not await svc.slots.delete(sess.email):
            raise HTTPException(status.HTTP_409_CONFLICT, detail="sem_instancia")
        await svc.access.forget_owner(sess.email)
        return await _status_response(svc, sess)

    @router.post("/api/power")
    async def power(body: PowerIn, svc: Svc, sess: WriteSession) -> JSONResponse:
        await _runtime(svc, sess).supervisor.set_power(body.on)
        return await _status_response(svc, sess)

    @router.post("/api/reload", status_code=status.HTTP_202_ACCEPTED)
    async def reload(svc: Svc, sess: WriteSession) -> Response:
        runtime = _runtime(svc, sess)
        if not runtime.store.power.desired_on:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="desligado")
        await runtime.supervisor.request_reload()
        return Response(status_code=status.HTTP_202_ACCEPTED)

    @router.post("/api/confirm/elsewhere")
    async def confirm_elsewhere(svc: Svc, sess: WriteSession) -> JSONResponse:
        """Modal: "sou eu, estou jogando em outro aparelho". Desliga sem entrar com a senha."""
        await _runtime(svc, sess).supervisor.confirm_elsewhere()
        return await _status_response(svc, sess)

    @router.post("/api/confirm/reconnect")
    async def confirm_reconnect(svc: Svc, sess: WriteSession) -> JSONResponse:
        """Modal: "não sou eu". Entra de novo sem esperar o fim da janela."""
        if not await _runtime(svc, sess).supervisor.reconnect_now():
            raise HTTPException(status.HTTP_409_CONFLICT, detail="sem_confirmacao")
        return await _status_response(svc, sess)

    @router.post("/api/retry-login", status_code=status.HTTP_202_ACCEPTED)
    async def retry_login(svc: Svc, sess: WriteSession) -> Response:
        await _runtime(svc, sess).supervisor.retry_login()
        return Response(status_code=status.HTTP_202_ACCEPTED)

    @router.get("/api/events")
    async def events(
        svc: Svc, sess: ReadSession, limit: Annotated[int, Query(ge=1, le=100)] = 50
    ) -> JSONResponse:
        runtime = svc.slots.for_email(sess.email)
        items = [] if runtime is None else [asdict(e) for e in runtime.store.events(limit)]
        return JSONResponse({"events": items}, headers={"Cache-Control": "no-store"})

    @router.get("/api/screenshot")
    async def screenshot(svc: Svc, sess: ReadSession) -> Response:
        runtime = _runtime(svc, sess)
        now = time.monotonic()
        if now - runtime.last_screenshot < SCREENSHOT_MIN_INTERVAL_S:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="devagar")
        runtime.last_screenshot = now
        try:
            image = await runtime.driver.screenshot()
        except (PlaywrightError, TimeoutError) as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="sem imagem") from exc
        if image is None:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="navegador fechado")
        return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # --- acesso à instância de outro jogador ----------------------------------------------------

    @router.post("/api/access/requests")
    async def request_access(body: AccessRequestIn, svc: Svc, sess: WriteSession) -> JSONResponse:
        """Admin pede a tela remota de outra instância: o link vai ao WhatsApp do dono dela."""
        try:
            await svc.access.request(sess.email, body.slot)
        except AccessError as exc:
            raise _access_refused(exc) from exc
        return await _status_response(svc, sess)

    @router.delete("/api/access/requests/{request_id}")
    async def cancel_request(request_id: AccessId, svc: Svc, sess: WriteSession) -> JSONResponse:
        try:
            await svc.access.cancel(sess.email, request_id)
        except AccessError as exc:
            raise _access_refused(exc) from exc
        return await _status_response(svc, sess)

    @router.post("/api/access/requests/{request_id}/approve")
    async def approve_request(
        request_id: AccessId, body: ApproveIn, svc: Svc, sess: WriteSession
    ) -> JSONResponse:
        """Só o dono da instância (pela sessão Google dele) libera."""
        try:
            await svc.access.approve(sess.email, request_id, body.hours)
        except AccessError as exc:
            raise _access_refused(exc) from exc
        return await _status_response(svc, sess)

    @router.post("/api/access/requests/{request_id}/deny")
    async def deny_request(request_id: AccessId, svc: Svc, sess: WriteSession) -> JSONResponse:
        try:
            await svc.access.deny(sess.email, request_id)
        except AccessError as exc:
            raise _access_refused(exc) from exc
        return await _status_response(svc, sess)

    @router.delete("/api/access/grants/{grant_id}")
    async def revoke_grant(grant_id: AccessId, svc: Svc, sess: WriteSession) -> JSONResponse:
        """O dono revoga, ou quem recebeu abre mão. A tela remota aberta cai na hora."""
        try:
            await svc.access.revoke(sess.email, grant_id)
        except AccessError as exc:
            raise _access_refused(exc) from exc
        return await _status_response(svc, sess)

    @router.get("/api/access/link/{token}")
    async def access_link(token: LinkToken, svc: Svc, sess: ReadSession) -> JSONResponse:
        """O pedido por trás do link do WhatsApp, para a página /acesso. Só para o dono."""
        headers = {"Cache-Control": "no-store"}
        try:
            req = svc.access.resolve_link(sess.email, token)
        except AccessError as exc:
            return JSONResponse(
                {"detail": exc.code, "email": sess.email, "csrf": sess.csrf},
                status_code=ACCESS_ERROR_STATUS.get(exc.code, status.HTTP_409_CONFLICT),
                headers=headers,
            )
        own = svc.slots.for_id(req.slot_id)
        hunter = None if own is None else own.supervisor.status().game_username or None
        payload = {
            "email": sess.email,
            "csrf": sess.csrf,
            "request": {
                "id": req.id,
                "requester": svc.access.label(req.requester),
                "hunter": hunter,
                "created_at": iso_z(req.created_at),
                "expires_at": iso_z(req.expires_at),
            },
        }
        return JSONResponse(payload, headers=headers)

    @router.get("/acesso/{token}", response_class=HTMLResponse)
    async def access_page(token: LinkToken, request: Request, svc: Svc) -> Response:
        """Link do WhatsApp. Sem sessão, passa pelo Google e volta aqui."""
        if svc.auth.session_for(request.cookies.get(SESSION_COOKIE)) is None:
            query = urlencode({"next": f"/acesso/{token}"})
            return RedirectResponse(f"/auth/login?{query}", status_code=status.HTTP_303_SEE_OTHER)
        return FileResponse(WEB_DIR / "acesso.html", media_type="text/html", headers=PAGE_HEADERS)

    # --- tela remota (noVNC) ----------------------------------------------------------------------

    @router.get("/remota", response_class=HTMLResponse)
    async def remote_screen(svc: Svc, sess: ReadSession, slot: SlotQuery = None) -> Response:
        """Sem `slot`: a própria instância. Com `slot` de outro jogador: só com acesso liberado."""
        try:
            svc.access.authorize_view(sess.email, slot)
        except AccessError:
            return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        return FileResponse(WEB_DIR / "remota.html", media_type="text/html", headers=PAGE_HEADERS)

    @router.get("/api/remote")
    async def remote_info(svc: Svc, sess: ReadSession, slot: SlotQuery = None) -> JSONResponse:
        """De quem é a tela e até quando vale; a tela remota consulta ao abrir e quando cai."""
        try:
            runtime, grant = svc.access.authorize_view(sess.email, slot)
        except AccessError as exc:
            raise _access_refused(exc) from exc
        record = runtime.record
        payload = {
            "slot_id": record.id,
            "own": grant is None,
            "name": runtime.supervisor.status().game_username or None,
            "email": record.email,
            "expires_at": None if grant is None else iso_z(grant.expires_at),
        }
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @router.get("/vnc/")
    async def vnc_home(svc: Svc, sess: ReadSession) -> Response:
        _runtime(svc, sess)
        return RedirectResponse(REMOTE_URL, status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/vnc/padrao")
    async def vnc_stock(svc: Svc, sess: ReadSession) -> Response:
        """A UI padrão do noVNC, de reserva."""
        _runtime(svc, sess)
        return RedirectResponse(
            f"/vnc/vnc.html?{NOVNC_QUERY}", status_code=status.HTTP_303_SEE_OTHER
        )

    @router.get("/vnc/{path:path}")
    async def vnc_files(path: str, svc: Svc, _s: ReadSession) -> Response:
        target = await asyncio.to_thread(_safe_file, settings.novnc_dir, path)
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        return FileResponse(target, headers={"Cache-Control": "no-cache"})

    @router.websocket("/vnc/ws")
    async def vnc_ws(ws: WebSocket) -> None:
        svc: Services = ws.app.state.services
        await bridge(ws, settings, svc.auth, svc.access)

    return router


async def _status_response(svc: Services, sess: Session) -> JSONResponse:
    used = svc.slots.max_slots - svc.slots.capacity_left()
    payload: dict[str, Any] = {
        "email": sess.email,
        "csrf": sess.csrf,
        "capacity": {"used": used, "max": svc.slots.max_slots},
        "slot": None,
    }
    runtime = svc.slots.for_email(sess.email)
    if runtime is not None:
        accounts = await svc.slots.accounts_view(runtime)
        fields = asdict(runtime.supervisor.status())
        fields.pop("game_username", None)
        payload.update(fields)
        payload["slot"] = {
            "id": runtime.record.id,
            "accounts": [asdict(a) for a in accounts],
            "max_accounts": MAX_ACCOUNTS,
        }
        payload["vnc_url"] = REMOTE_URL
    payload["access"] = svc.access.view(sess.email)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


def _safe_file(base: Path, path: str) -> Path | None:
    root = base.resolve()
    try:
        target = (root / path).resolve()
    except (OSError, ValueError):
        return None
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target


def _error_page(exc: LoginError) -> HTMLResponse:
    message = html.escape(LOGIN_ERRORS.get(exc.code, "Não foi possível entrar."))
    body = (
        '<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Leveling Keeper</title><link rel="stylesheet" href="/static/styles.css"></head>'
        '<body class="login"><main class="card"><h1>Leveling Keeper</h1>'
        f'<p class="error">{message}</p>'
        '<p><a class="button" href="/">Voltar</a></p></main></body></html>'
    )
    return HTMLResponse(body, status_code=exc.status_code, headers=PAGE_HEADERS)
