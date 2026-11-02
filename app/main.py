"""Aplicação: sobe a tela virtual, o Playwright, o supervisor e o painel; desce na ordem inversa.

O supervisor morrer por exceção inesperada derruba o /healthz (503) e o Swarm reinicia o container,
em vez de o painel ficar no ar com o robô parado.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from playwright.async_api import async_playwright

from app.api import Services, build_router
from app.auth import AuthService
from app.config import Settings
from app.display import Display
from app.game import GameDriver
from app.oidc import OidcClient
from app.state import StateStore
from app.supervisor import Supervisor

logger = logging.getLogger("app")


def _log_supervisor_end(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("supervisor stopped: %r", exc, exc_info=exc)


def create_app(settings: Settings | None = None, prebuilt: Services | None = None) -> FastAPI:
    cfg = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if prebuilt is not None:  # testes: componentes falsos, nada em segundo plano
            app.state.services = prebuilt
            yield
            return

        display = Display(cfg) if cfg.manage_display else None
        if display is not None:
            await display.start()
        pw = await async_playwright().start()
        driver = GameDriver(cfg, pw)
        store = StateStore(cfg.data_dir / "state.json")
        await store.load()
        auth = AuthService(cfg, OidcClient(cfg))
        await auth.load()
        supervisor = Supervisor(store, driver, cfg.secrets_file, tick_seconds=cfg.tick_seconds)
        task = asyncio.create_task(supervisor.run(), name="supervisor")
        task.add_done_callback(_log_supervisor_end)
        app.state.services = Services(store, supervisor, auth, driver, display, task)
        logger.info("started (desired_on=%s)", store.power.desired_on)
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # Fecha o Chromium sem mexer na chave: depois do redeploy ele volta como estava.
            await driver.close()
            await pw.stop()
            if display is not None:
                await display.stop()
            logger.info("stopped")

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(build_router(cfg))
    return app


def _configure_logging() -> None:
    logging.basicConfig(
        level=Settings().log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


_configure_logging()
app = create_app()
