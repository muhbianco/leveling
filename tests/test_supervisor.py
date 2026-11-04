from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config import Secrets, read_secrets
from app.policy import (
    CONFIRM_WINDOW_SECONDS,
    MAX_ATTEMPTS_PER_HOUR,
    Action,
    BlockKind,
    Observation,
    Phase,
    Screen,
)
from app.state import OffReason, StateStore
from app.supervisor import LOGIN_SPACING_S, MAX_LOGINS_BEFORE_LATCH, Supervisor


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeDriver:
    def __init__(self) -> None:
        self.obs = Observation(Screen.CLOSED)
        self.calls: list[str] = []
        self.logins: list[tuple[str, str]] = []

    async def observe(self) -> Observation:
        return self.obs

    async def launch(self) -> None:
        self.calls.append("launch")
        self.obs = Observation(Screen.LOADING)

    async def reload(self) -> None:
        self.calls.append("reload")

    async def login(self, username: str, password: str) -> None:
        self.calls.append("login")
        self.logins.append((username, password))

    async def close(self) -> None:
        self.calls.append("close")
        self.obs = Observation(Screen.CLOSED)

    async def forget_session(self) -> None:
        self.calls.append("forget")
        self.obs = Observation(Screen.CLOSED)

    async def screenshot(self) -> bytes | None:
        return None


@pytest.fixture
def secrets_file(tmp_path: Path) -> Path:
    path = tmp_path / "secrets.env"
    path.write_text("GAME_USERNAME=hunter\nGAME_PASSWORD=pw1\n", encoding="utf-8")
    return path


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def driver() -> FakeDriver:
    return FakeDriver()


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.json")


@pytest.fixture
def sup(
    store: StateStore, driver: FakeDriver, secrets_file: Path, clock: FakeClock
) -> Iterator[Supervisor]:
    async def credentials() -> Secrets:
        return read_secrets(secrets_file)

    yield Supervisor(store, driver, credentials, clock=clock, rng=lambda: 0.5)


PLAYING = Observation(Screen.GAME, signal_age=1.0, screen_age=600.0)


async def test_on_launches_and_reaches_playing(sup: Supervisor, driver: FakeDriver) -> None:
    await sup.set_power(True)
    assert (await sup.tick()).action is Action.LAUNCH
    driver.obs = PLAYING
    d = await sup.tick()
    assert (d.action, d.phase) == (Action.NONE, Phase.JOGANDO)
    assert sup.status().retry_in is None


async def test_replaced_turns_off_and_stays_off(
    sup: Supervisor, driver: FakeDriver, store: StateStore
) -> None:
    await sup.set_power(True)
    driver.obs = Observation(
        Screen.BLOCK, block_kind=BlockKind.REPLACED, block_text="outro aparelho"
    )
    d = await sup.tick()
    assert d.action is Action.TURN_OFF
    assert store.power.desired_on is False
    assert store.power.off_reason is OffReason.REPLACED
    assert driver.calls[-1] == "close"
    # Próximo ciclo: nada de reabrir (sem pingue-pongue com o aparelho do dono).
    d = await sup.tick()
    assert (d.action, d.phase) == (Action.NONE, Phase.ASSUMIDO)
    assert "launch" not in driver.calls


async def test_backoff_between_attempts(
    sup: Supervisor, driver: FakeDriver, clock: FakeClock
) -> None:
    await sup.set_power(True)
    await sup.tick()  # launch
    driver.obs = Observation(Screen.CLOSED)  # Chromium morreu logo
    assert (await sup.tick()).action is Action.NONE  # backoff de 30 s ainda correndo
    clock.now += 31
    assert (await sup.tick()).action is Action.LAUNCH
    driver.obs = Observation(Screen.CLOSED)  # morreu de novo
    clock.now += 31
    assert (await sup.tick()).action is Action.NONE  # agora 60 s
    clock.now += 30
    assert (await sup.tick()).action is Action.LAUNCH


async def test_hourly_cap_turns_off(
    sup: Supervisor, driver: FakeDriver, clock: FakeClock, store: StateStore
) -> None:
    await sup.set_power(True)
    for _ in range(MAX_ATTEMPTS_PER_HOUR):
        driver.obs = Observation(Screen.CLOSED)
        sup._retry_at = 0.0  # pula o backoff: o teste é do teto
        assert (await sup.tick()).action is Action.LAUNCH
        clock.now += 60
    driver.obs = Observation(Screen.CLOSED)
    sup._retry_at = 0.0
    d = await sup.tick()
    assert d.action is Action.TURN_OFF
    assert store.power.off_reason is OffReason.ERROR


async def test_login_spacing_and_latch(
    sup: Supervisor, driver: FakeDriver, clock: FakeClock, secrets_file: Path
) -> None:
    await sup.set_power(True)
    driver.obs = Observation(Screen.LOGIN)
    assert (await sup.tick()).action is Action.LOGIN
    assert driver.logins == [("hunter", "pw1")]
    assert (await sup.tick()).action is Action.NONE  # espaçamento
    for _ in range(MAX_LOGINS_BEFORE_LATCH - 1):
        clock.now += LOGIN_SPACING_S
        assert (await sup.tick()).action is Action.LOGIN
    clock.now += LOGIN_SPACING_S
    d = await sup.tick()
    assert (d.action, d.phase) == (Action.NONE, Phase.PRECISA_LOGIN)
    assert sup.status().login_latched is True

    # Trocar o arquivo de senha libera.
    secrets_file.write_text("GAME_USERNAME=hunter\nGAME_PASSWORD=pw2\n", encoding="utf-8")

    stat = secrets_file.stat()
    os.utime(secrets_file, (stat.st_atime, stat.st_mtime + 10))
    assert (await sup.tick()).action is Action.LOGIN
    assert driver.logins[-1] == ("hunter", "pw2")


async def test_retry_login_from_panel(
    sup: Supervisor, driver: FakeDriver, clock: FakeClock
) -> None:
    await sup.set_power(True)
    driver.obs = Observation(Screen.LOGIN)
    for _ in range(MAX_LOGINS_BEFORE_LATCH):
        await sup.tick()
        clock.now += LOGIN_SPACING_S
    assert (await sup.tick()).phase is Phase.PRECISA_LOGIN
    await sup.retry_login()
    assert (await sup.tick()).action is Action.LOGIN


async def test_reaching_game_resets_login_count(
    sup: Supervisor, driver: FakeDriver, clock: FakeClock
) -> None:
    await sup.set_power(True)
    driver.obs = Observation(Screen.LOGIN)
    assert (await sup.tick()).action is Action.LOGIN  # primeiro login depois de ligar: livre
    for _ in range(MAX_LOGINS_BEFORE_LATCH):
        driver.obs = PLAYING
        await sup.tick()
        clock.now += LOGIN_SPACING_S
        driver.obs = Observation(Screen.LOGIN)
        assert (await sup.tick()).action is Action.HOLD  # já jogou: pergunta antes
        clock.now += CONFIRM_WINDOW_SECONDS
        assert (await sup.tick()).action is Action.LOGIN
    assert sup.status().login_latched is False


EXPIRED = Observation(
    Screen.BLOCK,
    block_kind=BlockKind.RELOGIN,
    block_text="Sessão encerrada Sessão expirada. Entre novamente. ENTRAR NOVAMENTE",
    signal_age=2.0,
    screen_age=0.0,
)


async def _play_then_expire(sup: Supervisor, driver: FakeDriver) -> None:
    await sup.set_power(True)
    await sup.tick()  # launch
    driver.obs = PLAYING
    await sup.tick()
    driver.obs = EXPIRED


async def test_expired_after_playing_waits_before_logging_in(
    sup: Supervisor, driver: FakeDriver, clock: FakeClock, store: StateStore
) -> None:
    """Regressão de 04/11/2026: o dono voltou ao PC, o jogo mostrou "Sessão expirada" ao robô em
    vez de "acessada em outro dispositivo", e o robô entrou com a senha na hora e derrubou o PC."""
    await _play_then_expire(sup, driver)
    driver.calls.clear()
    d = await sup.tick()
    assert (d.action, d.phase) == (Action.HOLD, Phase.CONFIRMANDO)
    assert sup.status().confirm_in == CONFIRM_WINDOW_SECONDS
    assert store.events(1)[0].kind == "confirmar"

    clock.now += CONFIRM_WINDOW_SECONDS - 1
    d = await sup.tick()
    assert (d.action, d.phase) == (Action.NONE, Phase.CONFIRMANDO)
    assert driver.calls == []  # nem recarga nem login dentro da janela

    # Ninguém respondeu: recarrega, e o login que vem depois não pergunta de novo.
    clock.now += 1
    assert (await sup.tick()).action is Action.RELOAD
    assert store.events(2)[1].message.startswith("Ninguém confirmou")
    assert sup.status().confirm_in is None
    driver.obs = Observation(Screen.LOGIN)
    assert (await sup.tick()).action is Action.LOGIN

    # Voltou a jogar: a próxima queda pergunta de novo.
    driver.obs = PLAYING
    await sup.tick()
    driver.obs = EXPIRED
    assert (await sup.tick()).action is Action.HOLD


async def test_owner_confirms_playing_elsewhere(
    sup: Supervisor, driver: FakeDriver, store: StateStore
) -> None:
    await _play_then_expire(sup, driver)
    await sup.tick()  # hold
    await sup.confirm_elsewhere()
    assert (store.power.desired_on, store.power.off_reason) == (False, OffReason.REPLACED)
    assert store.events(1)[0].message == (
        "Desligado: você confirmou que está jogando em outro aparelho"
    )
    assert sup.status().confirm_in is None
    assert (await sup.tick()).action is Action.CLOSE
    assert driver.logins == []


async def test_owner_skips_the_window(sup: Supervisor, driver: FakeDriver) -> None:
    await _play_then_expire(sup, driver)
    assert await sup.reconnect_now() is False  # sem janela aberta
    await sup.tick()  # hold
    assert await sup.reconnect_now() is True
    assert (await sup.tick()).action is Action.RELOAD


async def test_window_closes_when_the_game_comes_back(sup: Supervisor, driver: FakeDriver) -> None:
    await _play_then_expire(sup, driver)
    await sup.tick()  # hold
    driver.obs = PLAYING
    assert (await sup.tick()).phase is Phase.JOGANDO
    assert sup.status().confirm_in is None


async def test_turning_on_again_starts_without_window(sup: Supervisor, driver: FakeDriver) -> None:
    await _play_then_expire(sup, driver)
    await sup.tick()  # hold
    await sup.set_power(False)
    await sup.tick()  # close
    await sup.set_power(True)
    driver.obs = Observation(Screen.LOGIN)
    assert (await sup.tick()).action is Action.LOGIN


async def test_remote_viewer_pauses_actions(sup: Supervisor, driver: FakeDriver) -> None:
    await sup.set_power(True)
    driver.obs = Observation(Screen.LOGIN)
    sup.viewer_joined()
    assert (await sup.tick()).action is Action.NONE
    sup.viewer_left()
    assert (await sup.tick()).action is Action.LOGIN


async def test_reload_requested_from_panel(sup: Supervisor, driver: FakeDriver) -> None:
    await sup.set_power(True)
    driver.obs = PLAYING
    await sup.tick()
    await sup.request_reload()
    assert (await sup.tick()).action is Action.RELOAD
    assert (await sup.tick()).action is Action.NONE


async def test_no_credentials_needs_owner(
    sup: Supervisor, driver: FakeDriver, secrets_file: Path
) -> None:
    secrets_file.write_text("", encoding="utf-8")
    await sup.set_power(True)
    driver.obs = Observation(Screen.LOGIN)
    d = await sup.tick()
    assert (d.action, d.phase) == (Action.NONE, Phase.PRECISA_LOGIN)
    assert driver.logins == []


async def test_reconnects_counted_after_playing(
    sup: Supervisor, driver: FakeDriver, clock: FakeClock
) -> None:
    await sup.set_power(True)
    await sup.tick()  # launch: não é reconexão
    driver.obs = PLAYING
    await sup.tick()
    driver.obs = Observation(Screen.GAME, signal_age=300.0, screen_age=600.0)
    assert (await sup.tick()).action is Action.RELOAD
    assert sup.status().reconnects_24h == 1
