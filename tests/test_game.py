from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import Settings
from app.game import CHROMIUM_ARGS, GameDriver
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
