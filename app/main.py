"""Aplicação: sobe o Playwright, uma instância por slot (tela + Chromium + supervisor) e o painel;
desce na ordem inversa.

Um supervisor morrer por exceção inesperada derruba o /healthz (503) e o Swarm reinicia o
container, em vez de o painel ficar no ar com o robô parado.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from playwright.async_api import Playwright, async_playwright

from app.access import AccessService
from app.api import Services, build_router
from app.auth import AuthService
from app.config import Settings
from app.display import Display
from app.game import GameDriver
from app.oidc import OidcClient
from app.slots import (
    AccountStore,
    RuntimeFactory,
    SlotManager,
    SlotRecord,
    SlotRegistry,
    SlotRuntime,
    credentials_provider,
)
from app.state import StateStore
from app.supervisor import Supervisor
from app.takeover import TakeoverRelay, TakeoverService
from app.whatsapp import WuzapiMessenger

logger = logging.getLogger("app")


def _log_supervisor_end(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("supervisor %s stopped: %r", task.get_name(), exc, exc_info=exc)


def runtime_factory(
    cfg: Settings,
    pw: Playwright,
    registry: SlotRegistry,
    accounts: AccountStore,
    relay: TakeoverRelay,
) -> RuntimeFactory:
    async def build(record: SlotRecord) -> SlotRuntime:
        slot_dir = registry.slot_dir(record.id)
        display_name = cfg.display_for(record.id)
        vnc_port = cfg.vnc_port_for(record.id)
        display = Display(cfg, display_name, vnc_port) if cfg.manage_display else None
        if display is not None:
            await display.start()
        driver = GameDriver(
            cfg, pw, slot_dir / "profile", display_name, proxy_pac=cfg.proxy_pac(record.id)
        )
        store = StateStore(slot_dir / "state.json")
        await store.load()
        supervisor = Supervisor(
            store,
            driver,
            credentials_provider(cfg, accounts, record),
            tick_seconds=cfg.tick_seconds,
            name=f"slot {record.id}",
            on_takeover=relay.for_slot(record.id),
        )
        task = asyncio.create_task(supervisor.run(), name=f"supervisor-{record.id}")
        task.add_done_callback(_log_supervisor_end)
        logger.info("slot %s started (desired_on=%s)", record.id, store.power.desired_on)
        return SlotRuntime(record, store, supervisor, driver, vnc_port, display, task)

    return build


def create_app(settings: Settings | None = None, prebuilt: Services | None = None) -> FastAPI:
    cfg = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if prebuilt is not None:  # testes: componentes falsos, nada em segundo plano
            app.state.services = prebuilt
            yield
            return

        pw = await async_playwright().start()
        registry = SlotRegistry(cfg.data_dir, cfg.max_slots)
        accounts = AccountStore(registry)
        relay = TakeoverRelay()
        factory = runtime_factory(cfg, pw, registry, accounts, relay)
        slots = SlotManager(cfg, registry, accounts, factory)
        messenger = WuzapiMessenger(cfg)
        # Antes dos supervisores começarem: uma saída por "assumido" no boot já ganha a oferta.
        takeover = TakeoverService(cfg, slots, messenger)
        relay.target = takeover
        await slots.start()
        auth = AuthService(cfg, OidcClient(cfg))
        await auth.load()
        access = AccessService(cfg, slots, messenger)
        await access.load()
        app.state.services = Services(auth, slots, access, takeover)
        logger.info("started with %s/%s slots", slots.used, cfg.max_slots)
        try:
            yield
        finally:
            await access.stop()
            await takeover.stop()
            await slots.stop_all()
            await pw.stop()
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
