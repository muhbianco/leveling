"""Rotas do painel. Só /healthz e a tela de login ficam abertos; o resto exige a sessão Google."""

from __future__ import annotations

import asyncio
import html
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, Field, field_validator

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
from app.slots import DataKeyError, SlotFullError, SlotManager, SlotRuntime
from app.vnc import bridge

WEB_DIR = Path(__file__).parent / "web"
STATIC_FILES = {"app.js": "text/javascript", "styles.css": "text/css"}
SCREENSHOT_MIN_INTERVAL_S = 2.0
NOVNC_QUERY = urlencode({"path": "vnc/ws", "autoconnect": "1", "resize": "scale", "reconnect": "1"})

PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "Referrer-Policy": "no-referrer",
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


class PowerIn(BaseModel):
    on: bool


class CredentialsIn(BaseModel):
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


def read_session(request: Request, svc: Svc) -> Session:
    return svc.auth.require(request)


def write_session(request: Request, svc: Svc) -> Session:
    return svc.auth.require_write(request)


ReadSession = Annotated[Session, Depends(read_session)]
WriteSession = Annotated[Session, Depends(write_session)]


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
    async def login(svc: Svc) -> Response:
        try:
            url, binding = svc.auth.start_login()
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
            token, _sess = await svc.auth.finish_login(
                state=state, code=code, error=error, binding=request.cookies.get(FLOW_COOKIE)
            )
        except LoginError as exc:
            refused = _error_page(exc)
            refused.delete_cookie(FLOW_COOKIE, path="/auth", **cookie_kwargs())
            return refused
        response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
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

    @router.put("/api/slot/credentials")
    async def save_credentials(body: CredentialsIn, svc: Svc, sess: WriteSession) -> JSONResponse:
        try:
            await svc.slots.save_credentials(sess.email, body.username, body.password)
        except SlotFullError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="sem_vaga") from exc
        except DataKeyError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="sem_chave") from exc
        return await _status_response(svc, sess)

    @router.delete("/api/slot")
    async def delete_slot(svc: Svc, sess: WriteSession) -> JSONResponse:
        if not await svc.slots.delete(sess.email):
            raise HTTPException(status.HTTP_409_CONFLICT, detail="sem_instancia")
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

    # --- tela remota (noVNC) ----------------------------------------------------------------------

    @router.get("/vnc/")
    async def vnc_home(svc: Svc, sess: ReadSession) -> Response:
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
        await bridge(ws, settings, svc.auth, svc.slots)

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
        username, source = await svc.slots.credential_info(runtime)
        fields = asdict(runtime.supervisor.status())
        fields.pop("game_username", None)
        payload.update(fields)
        payload["slot"] = {"id": runtime.record.id, "username": username, "credentials": source}
        payload["vnc_url"] = f"/vnc/vnc.html?{NOVNC_QUERY}"
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
