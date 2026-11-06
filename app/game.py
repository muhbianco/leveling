"""O Chromium que mantém o jogo aberto, via Playwright.

Tudo aqui é observação passiva, menos o login: o sinal de vida vem dos eventos de WebSocket do
próprio jogo (CDP, a página não vê), os textos de bloqueio vêm das respostas de locale que o jogo
já baixa, e a sonda do DOM só lê. Nenhum script é injetado e nada é clicado dentro do jogo.
Seletores e textos do jogo ficam concentrados neste arquivo.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Page, Playwright, Response, WebSocket
from playwright.async_api import Error as PlaywrightError

from app.config import Settings
from app.policy import SIGNAL_STALE_SECONDS, BlockMatcher, Observation, Screen, locale_texts

logger = logging.getLogger(__name__)

NAV_TIMEOUT_MS = 60_000
ACTION_TIMEOUT_MS = 15_000
PROBE_TIMEOUT_S = 5.0
SCREENSHOT_TIMEOUT_MS = 10_000
CLOSE_TIMEOUT_S = 20.0

# Só leitura. `.slScreen.slAuth` é a tela inicial/login, `.slScreen.slBlock` o bloqueio
# ("Sessão encerrada", manutenção, falha) e o canvas do Phaser em #game aparece dentro do jogo.
PROBE_JS = """() => {
  const shown = (el) => !!el && el.getClientRects().length > 0
    && getComputedStyle(el).visibility !== 'hidden';
  const block = document.querySelector('.slScreen.slBlock');
  const auth = document.querySelector('.slScreen.slAuth');
  const form = document.querySelector('#slAuthForm');
  return {
    block: shown(block) ? (block.innerText || '').slice(0, 600) : null,
    login: shown(auth) || shown(form),
    canvas: !!document.querySelector('#game canvas'),
  };
}"""

LOGIN_FORM = "#slAuthForm"
LOGIN_ENTRY = ".slEntryActions .slEntryButton:not(.slEntryButtonPrimary)"  # "Tenho uma conta"
LOGIN_TAB = "#slAuthTab-login"
LOGIN_TAB_ON = "slAuthTabOn"
LOGIN_USER = "#slAuth-username"
LOGIN_PASSWORD = "#slAuth-password"  # noqa: S105 (seletor)
LOGIN_SUBMIT = "#slAuthForm button[type=submit]"

LOCALE_PATHS = ("/locales/pt-BR/auth.json", "/locales/pt-BR/errors.json")

CHROMIUM_ARGS = (
    "--window-position=0,0",
    "--mute-audio",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    # Sem o balão "restaurar" depois de um kill do container.
    "--hide-crash-restore-bubble",
    # A aba é a única coisa na tela e precisa rodar como aba em primeiro plano.
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    # Sem GPU no servidor: WebGL do Phaser pelo renderizador de software do próprio Chromium.
    "--enable-unsafe-swiftshader",
    # Tela cheia (como F11): o jogo ocupa os 1280x720 inteiros da tela remota, sem a barra de abas
    # e endereço roubando 88 px nem recebendo clique por engano. F11 na tela remota sai e volta.
    "--start-fullscreen",
)


class Driver(Protocol):
    async def observe(self) -> Observation: ...
    async def launch(self) -> None: ...
    async def reload(self) -> None: ...
    async def login(self, username: str, password: str) -> None: ...
    async def close(self) -> None: ...
    async def forget_session(self) -> None: ...
    async def screenshot(self) -> bytes | None: ...


def clear_profile_locks(profile: Path) -> None:
    """Um kill deixa SingletonLock/Socket/Cookie no perfil e o Chromium recusa abrir.
    Uma réplica só, update stop-first: ninguém mais usa este perfil."""
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        with contextlib.suppress(FileNotFoundError):
            (profile / name).unlink()


class GameDriver:
    def __init__(
        self,
        settings: Settings,
        playwright: Playwright,
        profile_dir: Path,
        display: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._pw = playwright
        self._profile = profile_dir
        self._display = display
        self._clock = clock
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None
        self._gone = False  # contexto fechou sem a gente pedir (Chromium morreu)
        self._crashed = False
        self._last_signal: float | None = None
        self._screen: Screen = Screen.CLOSED
        self._screen_since = clock()
        self._matcher = BlockMatcher.from_texts({})
        self._locale_files: dict[str, dict[str, Any]] = {}

    # --- ciclo de vida ------------------------------------------------------------------

    async def launch(self) -> None:
        await self.close()
        profile = self._profile
        await asyncio.to_thread(profile.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(clear_profile_locks, profile)
        width, height = self._settings.screen_width, self._settings.screen_height
        ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            no_viewport=True,
            locale="pt-BR",
            args=[*CHROMIUM_ARGS, f"--window-size={width},{height}"],
            # No Linux o idioma da interface vem do LANGUAGE (o --lang não vale): pt-BR como a
            # página, então o Chromium não oferece o Tradutor por cima do jogo.
            env={
                **os.environ,
                "DISPLAY": self._display,
                "LANGUAGE": "pt_BR:pt",
                "LANG": "pt_BR.UTF-8",
            },
            handle_sigint=False,
            handle_sigterm=False,
            handle_sighup=False,
            timeout=NAV_TIMEOUT_MS,
        )
        self._ctx = ctx
        self._gone = False
        ctx.on("close", self._on_context_close)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        for extra in ctx.pages:
            if extra is not page:
                await extra.close()
        self._attach(page)
        await self._open_game(page)

    async def reload(self) -> None:
        page = self._page
        if page is None or self._gone or self._crashed or page.is_closed():
            await self.launch()
            return
        await self._open_game(page)

    async def close(self) -> None:
        ctx, self._ctx, self._page = self._ctx, None, None
        self._last_signal = None
        self._crashed = False
        self._set_screen(Screen.CLOSED)
        if ctx is None or self._gone:
            self._gone = False
            return
        try:
            await asyncio.wait_for(ctx.close(), CLOSE_TIMEOUT_S)
        except (PlaywrightError, TimeoutError) as exc:
            logger.warning("browser close failed: %s", type(exc).__name__)

    async def forget_session(self) -> None:
        """Fecha o navegador e apaga o perfil: o token da conta anterior (localStorage) vai junto
        e a próxima abertura cai no login. Falha aqui sobe: um perfil apagado pela metade poderia
        reabrir a conta velha com o painel mostrando a nova."""
        await self.close()
        await asyncio.to_thread(_remove_profile, self._profile)

    async def _open_game(self, page: Page) -> None:
        self._crashed = False
        self._last_signal = None
        self._set_screen(Screen.LOADING)
        await page.goto(
            self._settings.game_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
        )

    # --- sinais passivos -------------------------------------------------------------------

    def _attach(self, page: Page) -> None:
        self._page = page
        page.on("websocket", self._on_websocket)
        page.on("response", self._on_response)
        page.on("crash", self._on_crash)

    def _on_context_close(self, ctx: BrowserContext) -> None:
        # Só o contexto atual conta: o fechamento de um anterior (nosso close) chega atrasado.
        if ctx is self._ctx:
            logger.warning("browser context closed unexpectedly")
            self._gone = True

    def _on_crash(self, _page: Page) -> None:
        logger.warning("game page crashed")
        self._crashed = True

    def _on_websocket(self, ws: WebSocket) -> None:
        if not urlsplit(ws.url).path.endswith("/ws"):
            return
        self._touch()
        ws.on("framereceived", self._on_frame)

    def _on_frame(self, _payload: str | bytes) -> None:
        self._touch()

    def _touch(self) -> None:
        self._last_signal = self._clock()

    def _on_response(self, response: Response) -> None:
        path = urlsplit(response.url).path
        if path in LOCALE_PATHS and response.ok:
            task = asyncio.get_running_loop().create_task(self._read_locale(path, response))
            task.add_done_callback(_log_task_error)

    async def _read_locale(self, path: str, response: Response) -> None:
        try:
            data = await response.json()
        except (PlaywrightError, ValueError) as exc:
            logger.info("locale %s unreadable: %s", path, type(exc).__name__)
            return
        if isinstance(data, dict):
            self._locale_files[path] = data
            self._matcher = BlockMatcher.from_texts(locale_texts(self._locale_files.values()))

    # --- observação ------------------------------------------------------------------------

    def _set_screen(self, screen: Screen) -> None:
        if screen is not self._screen:
            self._screen = screen
            self._screen_since = self._clock()

    async def observe(self) -> Observation:
        page = self._page
        if page is None or self._gone or page.is_closed():
            if self._ctx is not None:
                await self.close()
            return Observation(Screen.CLOSED)
        now = self._clock()
        signal_age = None if self._last_signal is None else now - self._last_signal
        if self._crashed:
            return Observation(Screen.LOADING, crashed=True, signal_age=signal_age)

        raw: Any = None
        try:
            raw = await asyncio.wait_for(page.evaluate(PROBE_JS), PROBE_TIMEOUT_S)
        except (PlaywrightError, TimeoutError):
            raw = None  # navegando, contexto trocado ou página travada: conta como carregando

        block_text = ""
        if isinstance(raw, dict) and isinstance(raw.get("block"), str):
            block_text = raw["block"]
            screen = Screen.BLOCK
        elif isinstance(raw, dict) and raw.get("login"):
            screen = Screen.LOGIN
        elif isinstance(raw, dict) and raw.get("canvas"):
            screen = Screen.GAME
        elif raw is None and self._screen is Screen.GAME and _fresh(signal_age):
            # Sonda sem resposta com o jogo na tela e o servidor mandando quadros: é a página
            # ocupada (CPU no teto), não carregando. Contar como carregando fazia a fase pular
            # jogando <-> abrindo a cada poucos segundos e recarregar o jogo à toa depois de 90 s.
            screen = Screen.GAME
        else:
            screen = Screen.LOADING
        self._set_screen(screen)
        return Observation(
            screen=screen,
            block_kind=self._matcher.classify(block_text) if screen is Screen.BLOCK else None,
            block_text=" ".join(block_text.split()),
            signal_age=signal_age,
            screen_age=now - self._screen_since,
        )

    # --- única interação: login ----------------------------------------------------------

    async def login(self, username: str, password: str) -> None:
        page = self._page
        if page is None:
            return
        form = page.locator(LOGIN_FORM)
        if not await form.is_visible():
            await page.locator(LOGIN_ENTRY).first.click(timeout=ACTION_TIMEOUT_MS)
            await form.wait_for(state="visible", timeout=ACTION_TIMEOUT_MS)
        tab = page.locator(LOGIN_TAB)
        if await tab.count() and LOGIN_TAB_ON not in (await tab.get_attribute("class") or ""):
            await tab.click(timeout=ACTION_TIMEOUT_MS)
        await page.locator(LOGIN_USER).fill(username, timeout=ACTION_TIMEOUT_MS)
        await page.locator(LOGIN_PASSWORD).fill(password, timeout=ACTION_TIMEOUT_MS)
        await page.locator(LOGIN_SUBMIT).click(timeout=ACTION_TIMEOUT_MS)

    async def screenshot(self) -> bytes | None:
        page = self._page
        if page is None or self._gone or page.is_closed():
            return None
        return await page.screenshot(type="jpeg", quality=60, timeout=SCREENSHOT_TIMEOUT_MS)


def _fresh(signal_age: float | None) -> bool:
    return signal_age is not None and signal_age <= SIGNAL_STALE_SECONDS


def _remove_profile(profile: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(profile)


def _log_task_error(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.warning("background task failed: %s", type(task.exception()).__name__)
