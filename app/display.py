"""Tela virtual de um slot: Xvfb (onde o Chromium desenha) e X0tigervnc (a tela remota do painel).

Os dois ficam vivos o tempo todo e são reiniciados se caírem. O servidor VNC só escuta em
127.0.0.1: quem chega nele é a ponte autenticada de `vnc.py`.

TigerVNC no lugar do x11vnc (06/11/2026, medido em docs/tela-remota.md): lê a tela pelo XDAMAGE
em vez de varrer o framebuffer, manda quadros sem esperar pedido do cliente (ContinuousUpdates: a
taxa não depende dos 230 ms de ida e volta até o jogador) e respeita um teto de quadros por
segundo. No laboratório: metade da latência do clique e 70 % menos CPU que o x11vnc.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from app.config import Settings

logger = logging.getLogger(__name__)

RESTART_DELAY_S = 2.0
STOP_TIMEOUT_S = 5.0
READY_TIMEOUT_S = 15.0


class Display:
    """Uma tela por slot: cada jogador só alcança o próprio servidor VNC."""

    def __init__(self, settings: Settings, display: str, vnc_port: int) -> None:
        self._settings = settings
        self._display = display
        self._vnc_port = vnc_port
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: list[asyncio.Task[None]] = []
        number = display.lstrip(":").split(".")[0]
        self._socket = Path(f"/tmp/.X11-unix/X{number}")  # noqa: S108 (caminho fixo do X)

    def _commands(self) -> dict[str, list[str]]:
        s = self._settings
        return {
            "Xvfb": [
                "Xvfb",
                self._display,
                "-screen",
                "0",
                f"{s.screen_width}x{s.screen_height}x24",
                "-nolisten",
                "tcp",
            ],
            "vnc": [
                "X0tigervnc",
                "-display",
                self._display,
                "-rfbport",
                str(self._vnc_port),
                "-localhost=1",
                # Sem senha de VNC: a sessão Google do painel é a autenticação (ponte vnc.py).
                "-SecurityTypes=None",
                "-AlwaysShared=1",
                # Teto de quadros: o jogo redesenha a tela inteira a 60 fps; 30 por segundo cortam a
                # banda pela metade (0,6 a 0,8 MB/s na pior tela) por +30 ms de latência.
                f"-FrameRate={s.vnc_frame_rate}",
                # O teto padrão (35 % de um núcleo) faria o servidor espaçar os polls sozinho.
                "-MaxProcessorUsage=100",
                "-desktop=Leveling Keeper",
            ],
        }

    async def start(self) -> None:
        commands = self._commands()
        self._tasks.append(asyncio.create_task(self._keep("Xvfb", commands["Xvfb"])))
        await self.wait_ready()
        self._tasks.append(asyncio.create_task(self._keep("vnc", commands["vnc"])))

    async def wait_ready(self) -> None:
        # O Xvfb não avisa quando está pronto: o sinal é o socket aparecer.
        async with asyncio.timeout(READY_TIMEOUT_S):
            while not await asyncio.to_thread(self._socket.exists):  # noqa: ASYNC110
                await asyncio.sleep(0.2)

    def alive(self) -> bool:
        proc = self._procs.get("Xvfb")
        return proc is not None and proc.returncode is None

    async def _keep(self, name: str, argv: list[str]) -> None:
        while True:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._procs[name] = proc
            code = await proc.wait()
            logger.warning("%s exited with code %s; restarting", name, code)
            await asyncio.sleep(RESTART_DELAY_S)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for proc in self._procs.values():
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), STOP_TIMEOUT_S)
                except TimeoutError:
                    proc.kill()
