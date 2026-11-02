"""Tela virtual do container: Xvfb (onde o Chromium desenha) e x11vnc (a tela remota do painel).

Os dois ficam vivos o tempo todo e são reiniciados se caírem. O x11vnc só escuta em 127.0.0.1:
quem chega nele é a ponte autenticada de `vnc.py`.
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
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: list[asyncio.Task[None]] = []
        number = settings.display.lstrip(":").split(".")[0]
        self._socket = Path(f"/tmp/.X11-unix/X{number}")  # noqa: S108 (caminho fixo do X)

    def _commands(self) -> dict[str, list[str]]:
        s = self._settings
        return {
            "Xvfb": [
                "Xvfb",
                s.display,
                "-screen",
                "0",
                f"{s.screen_width}x{s.screen_height}x24",
                "-nolisten",
                "tcp",
            ],
            "x11vnc": [
                "x11vnc",
                "-display",
                s.display,
                "-rfbport",
                str(s.vnc_port),
                "-localhost",
                "-forever",
                "-shared",
                "-nopw",
                "-quiet",
                "-noxdamage",
            ],
        }

    async def start(self) -> None:
        commands = self._commands()
        self._tasks.append(asyncio.create_task(self._keep("Xvfb", commands["Xvfb"])))
        await self.wait_ready()
        self._tasks.append(asyncio.create_task(self._keep("x11vnc", commands["x11vnc"])))

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
