"""Executor: observa o navegador a cada poucos segundos, pergunta a `policy.decide` e cumpre.

Também guarda o que é só do processo (fase atual, backoff, orçamento de login, quem está na tela
remota). A chave liga/desliga e os eventos ficam no `StateStore`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from playwright.async_api import Error as PlaywrightError

from app.config import Secrets
from app.game import Driver, LoginFormError
from app.policy import (
    CONFIRM_WINDOW_SECONDS,
    SIGNAL_STALE_SECONDS,
    Action,
    Context,
    Decision,
    Observation,
    Phase,
    Relogin,
    Screen,
    decide,
)
from app.state import OffReason, StateStore, utcnow_iso

logger = logging.getLogger(__name__)

BACKOFF_BASE_S = 30.0
BACKOFF_CAP_S = 900.0
# Depois de enviar o login, o jogo pode levar mais de 1 min carregando com a tela de login ainda
# à vista (medido em 02/11: 61 s e 78 s). Tentar de novo antes disso atropela o primeiro login.
LOGIN_SPACING_S = 120.0
MAX_LOGINS_BEFORE_LATCH = 3
HOUR_S = 3600.0
DAY_S = 86_400.0

# Fases em que "próxima tentativa" faz sentido no painel.
RETRYING_PHASES = {Phase.ABRINDO, Phase.RECONECTANDO, Phase.MANUTENCAO, Phase.ENTRANDO}
CONFIRM_WINDOW_LABEL = f"{CONFIRM_WINDOW_SECONDS:.0f} s"

# Fases que viram evento ao entrar nelas (as de liga/desliga já viram evento no StateStore).
NOTABLE_PHASES = {
    Phase.JOGANDO: ("jogando", "Jogando: conectado ao servidor do jogo"),
    Phase.RECONECTANDO: ("aviso", "Conexão com o jogo em dúvida"),
    Phase.MANUTENCAO: ("aviso", "Jogo em manutenção ou reiniciando"),
    Phase.PRECISA_LOGIN: ("aviso", "Precisa de login"),
}


@dataclass(frozen=True, slots=True)
class Status:
    desired_on: bool
    off_reason: str
    off_detail: str
    changed_at: str
    phase: str
    phase_since: str
    reason: str
    signal_age: float | None
    reconnects_24h: int
    remote_viewers: int
    has_credentials: bool
    game_username: str
    login_latched: bool
    retry_in: float | None
    # Segundos até o robô entrar de novo sem confirmação (janela aberta); None fora dela.
    confirm_in: float | None


class Supervisor:
    def __init__(
        self,
        store: StateStore,
        driver: Driver,
        credentials: Callable[[], Awaitable[Secrets]],
        tick_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
        name: str = "slot",
    ) -> None:
        self._name = name  # só para o log: os dois slots escrevem no mesmo stdout
        self._store = store
        self._driver = driver
        self._credentials = credentials
        self._tick_seconds = tick_seconds
        self._clock = clock
        self._rng = rng
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()

        self.phase = Phase.DESLIGADO
        self.phase_since = utcnow_iso()
        self.reason = ""
        self._last_obs = Observation(Screen.CLOSED)
        self._secrets = Secrets()

        self._attempts: deque[float] = deque(maxlen=64)  # abrir/recarregar desde o último "jogando"
        self._failures = 0
        self._retry_at = 0.0
        self._played_since_on = False
        self._reconnects: deque[float] = deque(maxlen=2000)

        self._last_login_at = -math.inf
        self._logins_since_game = 0
        self._login_latched = False
        self._latched_version = 0.0

        # Janela de confirmação antes de entrar com a senha depois de já ter jogado.
        self._confirm_deadline: float | None = None
        self._relogin_cleared = False

        self._viewers = 0
        self._force_reload = False

    # --- laço -------------------------------------------------------------------------------

    async def run(self) -> None:
        while True:
            await self.tick()
            timeout = self._tick_seconds
            if self._confirm_deadline is not None:
                # Acorda no fim da janela, não no próximo ciclo cheio.
                timeout = max(0.2, min(timeout, self._confirm_deadline - self._clock()))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            self._wake.clear()

    def wake(self) -> None:
        self._wake.set()

    async def tick(self) -> Decision:
        async with self._lock:
            self._secrets = await self._credentials()
            if self._login_latched and self._secrets.version != self._latched_version:
                self._reset_login()
                await self._store.add_event("login", "Senha do jogo mudou: login liberado de novo")

            obs = await self._driver.observe()
            self._last_obs = obs
            now = self._clock()
            playing = (
                obs.screen is Screen.GAME
                and obs.signal_age is not None
                and obs.signal_age <= SIGNAL_STALE_SECONDS
            )
            if playing:
                self._attempts.clear()
                self._failures = 0
                self._retry_at = 0.0
                self._logins_since_game = 0
                self._played_since_on = True
                self._reset_confirm()
            elif self._confirm_deadline is not None and now >= self._confirm_deadline:
                self._confirm_deadline = None
                self._relogin_cleared = True
                await self._store.add_event(
                    "login", f"Ninguém confirmou em {CONFIRM_WINDOW_LABEL}: entrando de novo"
                )

            if not self._login_latched and self._logins_since_game >= MAX_LOGINS_BEFORE_LATCH:
                self._login_latched = True
                self._latched_version = self._secrets.version
                await self._store.add_event(
                    "erro",
                    f"Login recusado {MAX_LOGINS_BEFORE_LATCH} vezes: confira usuário e senha "
                    "e use 'Tentar login de novo'",
                )

            power = self._store.power
            ctx = Context(
                desired_on=power.desired_on,
                off_reason=power.off_reason,
                manual=self._viewers > 0,
                has_credentials=self._secrets.has_game_credentials,
                login_latched=self._login_latched,
                login_allowed=now - self._last_login_at >= LOGIN_SPACING_S,
                retry_ready=now >= self._retry_at,
                attempts_last_hour=sum(1 for t in self._attempts if t > now - HOUR_S),
                relogin=self._relogin_gate(now),
            )
            decision = decide(obs, ctx)
            if self._force_reload:
                self._force_reload = False
                if (
                    power.desired_on
                    and obs.screen is not Screen.CLOSED
                    and decision.action is Action.NONE
                ):
                    decision = Decision(Action.RELOAD, decision.phase, "pedido pelo painel")
            await self._enter(decision.phase, decision.reason)
            await self._execute(decision, now)
            return decision

    # --- ações ------------------------------------------------------------------------------

    async def _execute(self, d: Decision, now: float) -> None:
        match d.action:
            case Action.NONE:
                return
            case Action.HOLD:
                self._confirm_deadline = now + CONFIRM_WINDOW_SECONDS
                await self._store.add_event(
                    "confirmar",
                    f"A sessão caiu depois de jogar ({d.reason}). Se foi você em outro aparelho, "
                    f"confirme no painel; sem resposta em {CONFIRM_WINDOW_LABEL} o keeper entra "
                    "de novo",
                )
            case Action.LAUNCH | Action.RELOAD:
                await self._open(d, now)
            case Action.LOGIN:
                self._last_login_at = now
                self._logins_since_game += 1
                await self._store.add_event("login", "Entrando com usuário e senha")
                try:
                    await self._driver.login(
                        self._secrets.game_username, self._secrets.game_password
                    )
                except LoginFormError:
                    # Conta como tentativa (o teto de 3 continua valendo), mas nada foi enviado.
                    logger.warning("%s: login form did not hold the typed values", self._name)
                    await self._store.add_event(
                        "aviso",
                        "O formulário de login não ficou com o usuário e a senha digitados: "
                        "nada foi enviado, o keeper tenta de novo",
                    )
                except (PlaywrightError, TimeoutError) as exc:
                    # Só o tipo: mensagem de erro do Playwright não leva valor, mas não arriscamos.
                    logger.warning("%s: login step failed: %s", self._name, type(exc).__name__)
            case Action.CLOSE:
                await self._driver.close()
                self._reset_run()
            case Action.TURN_OFF:
                await self._store.set_power(False, d.off_reason or OffReason.ERROR, d.reason)
                await self._driver.close()
                self._reset_run()

    async def _open(self, d: Decision, now: float) -> None:
        self._attempts.append(now)
        self._failures += 1
        delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (self._failures - 1))
        self._retry_at = now + delay * (0.8 + 0.4 * self._rng())
        if self._played_since_on:
            self._reconnects.append(now)
        launching = d.action is Action.LAUNCH
        await self._store.add_event(
            "abrindo" if launching else "reconexao",
            "Abrindo o jogo" if launching else f"Recarregando o jogo: {d.reason}",
        )
        try:
            if launching:
                await self._driver.launch()
            else:
                await self._driver.reload()
        except (PlaywrightError, TimeoutError, OSError) as exc:
            logger.warning("%s: %s failed: %s", self._name, d.action.value, type(exc).__name__)
            await self._store.add_event("aviso", f"Falha ao abrir o jogo ({type(exc).__name__})")

    async def _enter(self, phase: Phase, reason: str) -> None:
        if phase is self.phase:
            self.reason = reason or self.reason
            return
        logger.info("%s: phase %s -> %s %s", self._name, self.phase.value, phase.value, reason)
        self.phase, self.phase_since, self.reason = phase, utcnow_iso(), reason
        notable = NOTABLE_PHASES.get(phase)
        if notable is not None:
            kind, message = notable
            await self._store.add_event(kind, f"{message}: {reason}" if reason else message)

    def _reset_run(self) -> None:
        self._attempts.clear()
        self._failures = 0
        self._retry_at = 0.0
        self._played_since_on = False
        self._reset_confirm()

    def _reset_confirm(self) -> None:
        self._confirm_deadline = None
        self._relogin_cleared = False

    def _relogin_gate(self, now: float) -> Relogin:
        """O primeiro login depois de ligar é livre (o dono acabou de sair do jogo). Depois de já
        ter jogado, o token cair quase sempre é o dono voltando: pede confirmação antes."""
        if not self._played_since_on or self._relogin_cleared:
            return Relogin.FREE
        if self._confirm_deadline is None:
            return Relogin.ASK
        return Relogin.WAIT if now < self._confirm_deadline else Relogin.FREE

    def _reset_login(self) -> None:
        self._login_latched = False
        self._logins_since_game = 0
        self._last_login_at = -math.inf

    # --- comandos do painel -------------------------------------------------------------------

    async def set_power(self, on: bool) -> None:
        changed = await self._store.set_power(on, OffReason.MANUAL)
        if changed and on:
            self._reset_run()
            self._reset_login()
        self.wake()

    async def request_reload(self) -> None:
        async with self._lock:
            self._retry_at = 0.0
            self._force_reload = True
        self.wake()

    async def retry_login(self) -> None:
        async with self._lock:
            self._reset_login()
        await self._store.add_event("login", "Login liberado de novo pelo painel")
        self.wake()

    async def credentials_changed(self) -> None:
        """Usuário/senha novos pelo painel: destrava o login (o evento é de quem gravou)."""
        async with self._lock:
            self._reset_login()
        self.wake()

    @contextlib.asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        """Segura o laço (nenhum ciclo roda no meio) para quem vai trocar a conta: lá dentro o
        chamador fecha o navegador e grava a conta nova. Backoff e orçamento de login eram da conta
        anterior e zeram; ao sair, o próximo ciclo já lê a conta nova."""
        try:
            async with self._lock:
                self._reset_run()
                self._reset_login()
                yield
        finally:
            # Deu certo ou não, o navegador pode ter fechado: o próximo ciclo vem já.
            self.wake()

    async def confirm_elsewhere(self) -> None:
        """O dono confirmou no painel que está jogando em outro aparelho: desliga sem entrar."""
        async with self._lock:
            self._reset_confirm()
        await self._store.set_power(
            False,
            OffReason.REPLACED,
            "confirmado no painel",
            message="Desligado: você confirmou que está jogando em outro aparelho",
        )
        self.wake()

    async def reconnect_now(self) -> bool:
        """O dono mandou entrar de novo sem esperar a janela. False se não havia janela aberta."""
        async with self._lock:
            if self._confirm_deadline is None:
                return False
            self._confirm_deadline = None
            self._relogin_cleared = True
        await self._store.add_event("login", "Reconexão liberada pelo painel")
        self.wake()
        return True

    def viewer_joined(self) -> None:
        self._viewers += 1

    def viewer_left(self) -> None:
        self._viewers = max(0, self._viewers - 1)
        self.wake()

    @property
    def viewers(self) -> int:
        return self._viewers

    def status(self) -> Status:
        now = self._clock()
        power = self._store.power
        return Status(
            desired_on=power.desired_on,
            off_reason=power.off_reason.value,
            off_detail=power.off_detail,
            changed_at=power.changed_at,
            phase=self.phase.value,
            phase_since=self.phase_since,
            reason=self.reason,
            signal_age=self._last_obs.signal_age,
            reconnects_24h=sum(1 for t in self._reconnects if t > now - DAY_S),
            remote_viewers=self._viewers,
            has_credentials=self._secrets.has_game_credentials,
            game_username=self._secrets.game_username,
            login_latched=self._login_latched,
            retry_in=(
                round(self._retry_at - now, 1)
                if self._retry_at > now and self.phase in RETRYING_PHASES
                else None
            ),
            confirm_in=(
                round(max(0.0, self._confirm_deadline - now), 1)
                if self._confirm_deadline is not None and power.desired_on
                else None
            ),
        )
