"""Ponte WebSocket <-> TCP entre o noVNC do painel e o x11vnc do slot (127.0.0.1:5900+N-1).

Faz o papel do websockify, mas atrás da sessão Google do painel: sem sessão (ou com Origin de
outro site) a conexão é recusada antes de abrir o TCP, e a porta vem do slot do e-mail da sessão,
então cada jogador só vê a própria tela. Enquanto alguém está na tela remota o supervisor do slot
entra em modo manual (não recarrega nem loga).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.auth import AuthService
from app.config import Settings
from app.slots import SlotManager

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 5.0
READ_CHUNK = 64 * 1024
POLICY_VIOLATION = 1008
TRY_AGAIN_LATER = 1013


async def bridge(ws: WebSocket, settings: Settings, auth: AuthService, slots: SlotManager) -> None:
    sess = auth.websocket_session(ws)
    runtime = None if sess is None else slots.for_email(sess.email)
    if runtime is None:
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
    logger.info("remote view opened")

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

    tasks = [asyncio.create_task(ws_to_tcp()), asyncio.create_task(tcp_to_ws())]
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
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        if ws.application_state is WebSocketState.CONNECTED:
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await ws.close()
        logger.info("remote view closed")
