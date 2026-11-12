from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.game import (
    CHROMIUM_ARGS,
    FILL_ROUNDS,
    LOGIN_ENTRY,
    LOGIN_PASSWORD,
    LOGIN_SUBMIT,
    LOGIN_TAB_ON,
    LOGIN_USER,
    PASSWORD_STORE_FILES,
    PROBE_JS,
    GameDriver,
    LoginFormError,
    silence_password_manager,
)
from app.policy import Screen
from tests.test_supervisor import FakeClock

IN_GAME = {"block": None, "login": False, "canvas": True}


class FakePage:
    """Só o que observe() usa: a sonda do DOM, que pode estourar o tempo com a CPU no teto."""

    def __init__(self) -> None:
        self.result: dict[str, Any] = IN_GAME
        self.fail = False

    def is_closed(self) -> bool:
        return False

    async def evaluate(self, _js: str) -> dict[str, Any]:
        if self.fail:
            raise TimeoutError
        return self.result


def driver_with_page(clock: FakeClock) -> tuple[GameDriver, FakePage]:
    driver = GameDriver(Settings(manage_display=False), None, Path("perfil"), ":99", clock=clock)  # type: ignore[arg-type]
    page = FakePage()
    driver._page = page  # type: ignore[assignment]
    return driver, page


async def test_probe_reports_a_saved_session_without_its_value() -> None:
    driver, page = driver_with_page(FakeClock())
    page.result = {"block": None, "login": True, "canvas": True, "token": True}
    obs = await driver.observe()
    assert (obs.screen, obs.token) == (Screen.LOGIN, True)
    page.result = {"block": None, "login": True, "canvas": False, "token": False}
    assert (await driver.observe()).token is False
    page.fail = True
    assert (await driver.observe()).token is False
    # A sonda só pergunta se a sessão existe: o valor nunca volta para o Python.
    assert "!!(sessionStorage.getItem(key) || localStorage.getItem(key))" in PROBE_JS


async def test_busy_page_with_live_socket_stays_in_game() -> None:
    """Regressão de 04/11/2026: com a CPU no teto, a sonda estourava o tempo, a fase pulava
    jogando <-> abrindo a cada poucos segundos e o jogo era recarregado à toa depois de 90 s."""
    clock = FakeClock()
    driver, page = driver_with_page(clock)
    driver._touch()
    assert (await driver.observe()).screen is Screen.GAME

    page.fail = True
    clock.now += 20
    driver._touch()  # o servidor continua mandando quadros
    clock.now += 5
    obs = await driver.observe()
    assert obs.screen is Screen.GAME
    assert obs.screen_age == 25  # a mesma tela desde o começo: nada de recontar do zero


async def test_busy_page_without_signal_counts_as_loading() -> None:
    clock = FakeClock()
    driver, page = driver_with_page(clock)
    driver._touch()
    assert (await driver.observe()).screen is Screen.GAME
    page.fail = True
    clock.now += 31  # sem quadro do servidor: pode ser a página caindo de verdade
    assert (await driver.observe()).screen is Screen.LOADING


async def test_failed_probe_before_the_game_is_still_loading() -> None:
    clock = FakeClock()
    driver, page = driver_with_page(clock)
    page.fail = True
    driver._touch()
    assert (await driver.observe()).screen is Screen.LOADING


def test_chromium_takes_the_whole_screen() -> None:
    """Tela cheia desde o início: o jogo fica com os 1280x720 da tela remota (sem a barra de abas,
    que roubava 88 px e recebia clique por engano). Medido no laboratório: viewport 719 px de altura
    com a flag, 632 sem."""
    assert "--start-fullscreen" in CHROMIUM_ARGS


def test_proxy_pac_becomes_a_chromium_flag() -> None:
    settings = Settings(manage_display=False, max_slots=3, egress_proxy="socks5://172.17.0.1:1080")
    direct = GameDriver(settings, None, Path("p1"), ":99", proxy_pac=settings.proxy_pac(1))  # type: ignore[arg-type]
    via_proxy = GameDriver(settings, None, Path("p3"), ":101", proxy_pac=settings.proxy_pac(3))  # type: ignore[arg-type]
    assert direct._proxy_pac is None
    assert via_proxy._proxy_pac is not None and via_proxy._proxy_pac.startswith("data:")


# --- login: o formulário do jogo, com alguém mais mexendo nos campos -----------------------------


class FakeLoginPage:
    """O formulário de login com um intruso: `meddle` devolve o que fica no campo depois de cada
    `fill` (o preenchimento automático do Chromium caindo entre o selecionar e o inserir)."""

    def __init__(self, visible: bool = True) -> None:
        self.visible = visible
        self.values = {LOGIN_USER: "", LOGIN_PASSWORD: ""}
        self.fills: list[str] = []
        self.clicks: list[str] = []
        self.submitted: list[tuple[str, str]] = []
        self.meddle: Callable[[str, str, int], str] = lambda _selector, typed, _nth: typed

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)


class FakeLocator:
    def __init__(self, page: FakeLoginPage, selector: str) -> None:
        self._page = page
        self._selector = selector

    @property
    def first(self) -> FakeLocator:
        return self

    async def is_visible(self) -> bool:
        return self._page.visible

    async def wait_for(self, **_kwargs: Any) -> None:
        assert self._page.visible

    async def count(self) -> int:
        return 1

    async def get_attribute(self, _name: str) -> str:
        return f"slAuthTab {LOGIN_TAB_ON}"

    async def input_value(self, **_kwargs: Any) -> str:
        return self._page.values[self._selector]

    async def fill(self, value: str, **_kwargs: Any) -> None:
        nth = self._page.fills.count(self._selector)
        self._page.fills.append(self._selector)
        self._page.values[self._selector] = self._page.meddle(self._selector, value, nth)

    async def click(self, **_kwargs: Any) -> None:
        self._page.clicks.append(self._selector)
        if self._selector == LOGIN_ENTRY:
            self._page.visible = True
        if self._selector == LOGIN_SUBMIT:
            values = self._page.values
            self._page.submitted.append((values[LOGIN_USER], values[LOGIN_PASSWORD]))


@pytest.fixture
def login_page(monkeypatch: pytest.MonkeyPatch) -> tuple[GameDriver, FakeLoginPage]:
    monkeypatch.setattr("app.game.FILL_SETTLE_S", 0.0)
    driver = GameDriver(Settings(manage_display=False), None, Path("perfil"), ":99")  # type: ignore[arg-type]
    page = FakeLoginPage()
    driver._page = page  # type: ignore[assignment]
    return driver, page


async def test_login_types_once_and_submits(login_page: tuple[GameDriver, FakeLoginPage]) -> None:
    driver, page = login_page
    page.visible = False  # tela inicial: precisa do "Tenho uma conta"
    await driver.login("hunter", "Pw#12345")
    assert page.clicks == [LOGIN_ENTRY, LOGIN_SUBMIT]
    assert page.fills == [LOGIN_USER, LOGIN_PASSWORD]
    assert page.submitted == [("hunter", "Pw#12345")]


async def test_login_fixes_a_doubled_password_before_sending(
    login_page: tuple[GameDriver, FakeLoginPage],
) -> None:
    """Regressão de 10/11/2026: o Chromium tinha a senha salva e preenchia o campo no meio do
    `fill` do keeper. O campo ficava com a senha duas vezes, o keeper enviava assim mesmo e o
    login nunca entrava (painel preso em "entrando")."""
    driver, page = login_page
    page.meddle = lambda selector, typed, nth: (
        typed + typed if selector == LOGIN_PASSWORD and nth == 0 else typed
    )
    await driver.login("hunter", "Pw#12345")
    assert page.submitted == [("hunter", "Pw#12345")]
    # Só a senha foi redigitada: mexer no usuário de novo chamaria outro preenchimento.
    assert page.fills == [LOGIN_USER, LOGIN_PASSWORD, LOGIN_PASSWORD]


async def test_login_does_not_retype_fields_already_right(
    login_page: tuple[GameDriver, FakeLoginPage],
) -> None:
    driver, page = login_page
    page.values.update({LOGIN_USER: "hunter", LOGIN_PASSWORD: "senha velha"})
    await driver.login("hunter", "Pw#12345")
    assert page.fills == [LOGIN_PASSWORD]
    assert page.submitted == [("hunter", "Pw#12345")]


async def test_login_never_sends_what_it_did_not_type(
    login_page: tuple[GameDriver, FakeLoginPage],
) -> None:
    driver, page = login_page
    page.meddle = lambda selector, typed, _nth: (
        typed + typed if selector == LOGIN_PASSWORD else typed
    )
    with pytest.raises(LoginFormError):
        await driver.login("hunter", "Pw#12345")
    assert page.submitted == []
    assert LOGIN_SUBMIT not in page.clicks
    assert page.fills.count(LOGIN_PASSWORD) == FILL_ROUNDS  # insiste, mas com teto


# --- perfil: o gerenciador de senhas do Chromium fora do caminho ----------------------------------


def test_saved_passwords_are_removed_before_launch(tmp_path: Path) -> None:
    """Regressão de 10/11/2026: `Login Data` dos slots 1 e 2 guardava a senha do jogo (usada 55 e
    64 vezes pelo preenchimento automático). Com `--password-store=basic` ela fica no volume
    praticamente em claro, fora da cifragem do `accounts.json`."""
    default = tmp_path / "Default"
    default.mkdir()
    for name in PASSWORD_STORE_FILES:
        (default / name).write_bytes(b"sqlite")
    (default / "Web Data").write_bytes(b"fica")
    (default / "Preferences").write_text(
        json.dumps({"profile": {"name": "Pessoa 1"}, "credentials_enable_service": True}),
        encoding="utf-8",
    )

    silence_password_manager(tmp_path)

    assert sorted(p.name for p in default.iterdir()) == ["Preferences", "Web Data"]
    prefs = json.loads((default / "Preferences").read_text(encoding="utf-8"))
    assert prefs["credentials_enable_service"] is False
    assert prefs["credentials_enable_autosignin"] is False
    assert prefs["profile"] == {"name": "Pessoa 1"}  # o resto do perfil fica como estava


def test_new_profile_starts_with_password_manager_off(tmp_path: Path) -> None:
    silence_password_manager(tmp_path)  # perfil que o Chromium ainda não criou
    prefs = json.loads((tmp_path / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert prefs == {"credentials_enable_service": False, "credentials_enable_autosignin": False}


def test_broken_preferences_do_not_block_the_launch(tmp_path: Path) -> None:
    default = tmp_path / "Default"
    default.mkdir()
    (default / "Login Data").write_bytes(b"sqlite")
    (default / "Preferences").write_text('{"profile": {"na', encoding="utf-8")

    silence_password_manager(tmp_path)

    assert not (default / "Login Data").exists()  # o cofre sai mesmo assim
    assert (default / "Preferences").read_text(encoding="utf-8") == '{"profile": {"na'
