"""Ponte WebSocket <-> TCP entre o noVNC do painel e o X0tigervnc do slot (127.0.0.1:5900+N-1).

Faz o papel do websockify, mas atrás da sessão Google do painel: sem sessão (ou com Origin de
outro site) a conexão é recusada antes de abrir o TCP. Sem `?slot=`, a porta vem do slot do e-mail
da sessão; com o slot de outro jogador, só com acesso liberado pelo dono (app/access.py), e a
conexão cai sozinha quando o acesso é revogado ou vence. Enquanto alguém está na tela remota o
supervisor do slot entra em modo manual (não recarrega nem loga).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.access import AccessError, AccessService
from app.auth import AuthService
from app.config import Settings

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 5.0
CLOSE_TIMEOUT_S = 3.0
READ_CHUNK = 64 * 1024
POLICY_VIOLATION = 1008
TRY_AGAIN_LATER = 1013
# De quanto em quanto a ponte de quem tem acesso confere o prazo (além do aviso de revogação).
GRANT_RECHECK_S = 60.0


def _slot_param(ws: WebSocket) -> int | None:
    raw = ws.query_params.get("slot")
    if raw is None:
        return None
    if not raw.isdigit() or len(raw) > 2:
        raise AccessError("acesso_negado")
    return int(raw)


async def bridge(
    ws: WebSocket, settings: Settings, auth: AuthService, access: AccessService
) -> None:
    sess = auth.websocket_session(ws)
    if sess is None:
        await ws.close(code=POLICY_VIOLATION)
        return
    try:
        runtime, grant = access.authorize_view(sess.email, _slot_param(ws))
    except AccessError:
        await ws.close(code=POLICY_VIOLATION)
        return
    supervisor = runtime.supervisor
    if supervisor.viewers >= settings.vnc_max_clients:
        await ws.close(code=TRY_AGAIN_LATER)
        return
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(settings.vnc_host, runtime.vnc_port), CONNECT_TIMEOUT_S
        )
    except (OSError, TimeoutError) as exc:
        logger.warning("vnc unreachable: %s", type(exc).__name__)
        await ws.close(code=TRY_AGAIN_LATER)
        return

    subprotocol = "binary" if "binary" in ws.scope.get("subprotocols", []) else None
    await ws.accept(subprotocol=subprotocol)
    supervisor.viewer_joined()
    revoked = None if grant is None else access.watch(grant.id)
    if grant is not None:
        await access.event(runtime, f"{access.label(sess.email)} abriu a tela remota")
    logger.info("remote view opened (shared=%s)", grant is not None)

    async def ws_to_tcp() -> None:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is None and message.get("text") is not None:
                data = message["text"].encode()
            if data:
                writer.write(data)
                await writer.drain()

    async def tcp_to_ws() -> None:
        while True:
            data = await reader.read(READ_CHUNK)
            if not data:
                return
            await ws.send_bytes(data)

    async def guard() -> None:
        """Termina quando o acesso de quem não é o dono acaba (revogado, vencido, slot trocado)."""
        assert grant is not None and revoked is not None
        while True:
            left = access.grant_left(grant.id)
            if left is None or left <= 0:
                return
            try:
                await asyncio.wait_for(revoked.wait(), timeout=min(left, GRANT_RECHECK_S))
            except TimeoutError:
                continue
            return

    tasks = [asyncio.create_task(ws_to_tcp()), asyncio.create_task(tcp_to_ws())]
    if grant is not None:
        tasks.append(asyncio.create_task(guard()))
    try:
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(
                asyncio.CancelledError, WebSocketDisconnect, OSError, RuntimeError
            ):
                await task
    finally:
        supervisor.viewer_left()
        if grant is not None and revoked is not None:
            access.unwatch(grant.id, revoked)
        # O navegador fica sabendo primeiro; o TCP local fecha depois, com prazo (no Proactor do
        # Windows o wait_closed depois de um read cancelado pode não voltar).
        if ws.application_state is WebSocketState.CONNECTED:
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                # Acesso encerrado: 1008, para a tela remota não insistir em reconectar.
                ended = grant is not None and access.grant_left(grant.id) is None
                await ws.close(code=POLICY_VIOLATION if ended else 1000)
        writer.close()
        with contextlib.suppress(OSError, TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), CLOSE_TIMEOUT_S)
        if grant is not None:
            await access.event(runtime, f"{access.label(sess.email)} fechou a tela remota")
        logger.info("remote view closed")
