"""O Chromium que mantém o jogo aberto, via Playwright.

Tudo aqui é observação passiva, menos o login: o sinal de vida vem dos eventos de WebSocket do
próprio jogo (CDP, a página não vê), os textos de bloqueio vêm das respostas de locale que o jogo
já baixa, e a sonda do DOM só lê. Nenhum script é injetado e nada é clicado dentro do jogo.
Seletores e textos do jogo ficam concentrados neste arquivo.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
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
from app.state import write_json_atomic

logger = logging.getLogger(__name__)

NAV_TIMEOUT_MS = 60_000
ACTION_TIMEOUT_MS = 15_000
PROBE_TIMEOUT_S = 5.0
SCREENSHOT_TIMEOUT_MS = 10_000
CLOSE_TIMEOUT_S = 20.0

# O `fill` do Playwright são dois passos (seleciona o campo, depois insere o texto): quem mexer no
# campo entre um e outro deixa o texto dobrado. Por isso o login confere os campos antes de enviar
# e redigita o que estiver diferente, no máximo este número de vezes.
FILL_ROUNDS = 3
# Espera entre digitar e conferir: um preenchimento atrasado de terceiro aparece na conferência.
FILL_SETTLE_S = 0.4

# Cofre de senhas do Chromium, em <perfil>/Default. Com `--password-store=basic` a senha fica ali
# praticamente em claro, fora da cifragem do `accounts.json`.
PASSWORD_STORE_FILES = (
    "Login Data",
    "Login Data-journal",
    "Login Data For Account",
    "Login Data For Account-journal",
)
# Preferências do perfil: sem oferta de "Salvar senha?" e sem login automático do Chromium.
PASSWORD_MANAGER_PREFS = {
    "credentials_enable_service": False,
    "credentials_enable_autosignin": False,
}


class LoginFormError(Exception):
    """Os campos do login não ficaram com o usuário e a senha digitados. Nada foi enviado."""


# Só leitura. `.slScreen.slAuth` é a tela inicial/login, `.slScreen.slBlock` o bloqueio
# ("Sessão encerrada", manutenção, falha) e o canvas do Phaser em #game aparece dentro do jogo.
# `token`: o jogo tem uma sessão salva (sessionStorage ou localStorage). Só se existe, nunca o
# valor. O cliente grava ao entrar (onAuthOk) e apaga a cada fim de sessão (clearToken), lido do
# bundle em 12/11/2026.
PROBE_JS = """() => {
  const shown = (el) => !!el && el.getClientRects().length > 0
    && getComputedStyle(el).visibility !== 'hidden';
  const block = document.querySelector('.slScreen.slBlock');
  const auth = document.querySelector('.slScreen.slAuth');
  const form = document.querySelector('#slAuthForm');
  const key = 'leveling.session-token';
  let token = false;
  try { token = !!(sessionStorage.getItem(key) || localStorage.getItem(key)); } catch (e) {}
  return {
    block: shown(block) ? (block.innerText || '').slice(0, 600) : null,
    login: shown(auth) || shown(form),
    canvas: !!document.querySelector('#game canvas'),
    token,
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


def silence_password_manager(profile: Path) -> None:
    """O login é do keeper, não do gerenciador de senhas do Chromium.

    Regressão de 10/11/2026: o campo da senha apareceu com a senha duas vezes e o login não entrou.
    O Chromium tinha a senha do jogo salva no perfil dos slots 1 e 2 (o Playwright 1.64 não liga
    `--enable-automation`, então o gerenciador de senhas fica ativo) e preenchia o formulário por
    conta própria, na mesma hora em que o keeper digitava.

    A cada abertura, com o Chromium fechado: apaga o cofre (some a senha salva e, com ela, o
    preenchimento automático) e desliga a oferta de salvar. Falha de disco sobe para quem abre: é
    melhor não abrir do que abrir com a senha salva.
    """
    default = profile / "Default"
    for name in PASSWORD_STORE_FILES:
        with contextlib.suppress(FileNotFoundError):
            (default / name).unlink()

    path = default / "Preferences"
    try:
        prefs = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        prefs = {}  # perfil novo: o Chromium parte deste arquivo
    except ValueError:
        # Arquivo cortado: o Chromium recria sozinho. O cofre já saiu e o login confere os campos.
        logger.warning("profile preferences unreadable, left as is")
        return
    if not isinstance(prefs, dict):
        return
    if all(prefs.get(key) is value for key, value in PASSWORD_MANAGER_PREFS.items()):
        return
    prefs.update(PASSWORD_MANAGER_PREFS)
    write_json_atomic(path, prefs)


class GameDriver:
    def __init__(
        self,
        settings: Settings,
        playwright: Playwright,
        profile_dir: Path,
        display: str,
        clock: Callable[[], float] = time.monotonic,
        proxy_pac: str | None = None,
    ) -> None:
        self._settings = settings
        self._pw = playwright
        self._profile = profile_dir
        self._display = display
        self._clock = clock
        # Slot acima de DIRECT_SLOTS: o servidor do jogo vai pelo proxy de saída (segundo IP).
        self._proxy_pac = proxy_pac
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
        await asyncio.to_thread(silence_password_manager, profile)
        width, height = self._settings.screen_width, self._settings.screen_height
        args = [*CHROMIUM_ARGS, f"--window-size={width},{height}"]
        if self._proxy_pac:
            args.append(f"--proxy-pac-url={self._proxy_pac}")
        ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            no_viewport=True,
            locale="pt-BR",
            args=args,
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
            token=isinstance(raw, dict) and bool(raw.get("token")),
        )

    # --- única interação: login ----------------------------------------------------------

    async def login(self, username: str, password: str) -> None:
        """Só envia com os dois campos exatamente iguais ao usuário e à senha: confere depois de
        digitar e redigita só o campo que estiver diferente (mexer no usuário de novo poderia
        chamar outro preenchimento da senha). Se não firmar, sobe `LoginFormError` sem enviar:
        mandar uma senha errada só gasta tentativa e deixa o painel em "entrando" à toa."""
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
        fields = (
            (page.locator(LOGIN_USER), username),
            (page.locator(LOGIN_PASSWORD), password),
        )
        for _ in range(FILL_ROUNDS):
            for field, value in fields:
                if await field.input_value(timeout=ACTION_TIMEOUT_MS) != value:
                    await field.fill(value, timeout=ACTION_TIMEOUT_MS)
            await asyncio.sleep(FILL_SETTLE_S)
            held = True
            for field, value in fields:
                held = held and await field.input_value(timeout=ACTION_TIMEOUT_MS) == value
            if held:
                await page.locator(LOGIN_SUBMIT).click(timeout=ACTION_TIMEOUT_MS)
                return
        raise LoginFormError

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
