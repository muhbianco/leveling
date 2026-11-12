"""O dono assumiu o jogo em outro aparelho e o keeper saiu (policy.Relogin.YIELD, ou o bloqueio
"acessada em outro dispositivo"). Por OFFER_SECONDS o dono pode mandar o keeper assumir de novo:

- painel aberto (consultou o status há menos de PANEL_FRESH_SECONDS): modal no painel;
- painel fechado: link `/retomar/<token>` no WhatsApp do dono.

Sem resposta, nada acontece: o keeper continua desligado e o dono segue jogando. O keeper nunca
volta sozinho: uma conta, um aparelho (o jogo derruba os dois e bloqueia a conta por minutos).

O token do link só localiza a oferta: quem religa é a sessão Google do dono do slot. As ofertas
ficam só na memória (duram 5 min); um restart as descarta e o keeper continua desligado.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from app.config import Settings
from app.slots import SlotManager, SlotRuntime
from app.whatsapp import Messenger, SendResult

logger = logging.getLogger(__name__)

OFFER_SECONDS = 300.0
PANEL_FRESH_SECONDS = 15.0  # o painel consulta o status a cada 5 s, mesmo em segundo plano
VIA_PANEL = "painel"
VIA_SENDING = "enviando"
BRT = timezone(timedelta(hours=-3))


class TakeoverError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Offer:
    slot_id: int
    owner: str
    token_hash: str
    created_at: float
    expires_at: float
    via: str  # VIA_PANEL, VIA_SENDING ou um SendResult


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, BRT).strftime("%H:%M")


class TakeoverRelay:
    """Liga os supervisores (criados com os slots) ao serviço (criado depois deles)."""

    def __init__(self) -> None:
        self.target: TakeoverService | None = None

    def for_slot(self, slot_id: int) -> Callable[[str], Awaitable[None]]:
        async def hook(reason: str) -> None:
            if self.target is not None:
                await self.target.on_takeover(slot_id, reason)

        return hook


class TakeoverService:
    def __init__(
        self,
        settings: Settings,
        slots: SlotManager,
        messenger: Messenger,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._slots = slots
        self._messenger = messenger
        self._clock = clock
        self._monotonic = monotonic
        self._offers: dict[int, Offer] = {}
        self._panel_seen: dict[str, float] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # --- presença do painel -----------------------------------------------------------------

    def panel_seen(self, email: str) -> None:
        self._panel_seen[email] = self._monotonic()

    def _panel_open(self, email: str) -> bool:
        seen = self._panel_seen.get(email)
        return seen is not None and self._monotonic() - seen < PANEL_FRESH_SECONDS

    # --- oferta -----------------------------------------------------------------------------

    async def on_takeover(self, slot_id: int, reason: str) -> None:
        """O keeper do slot acabou de sair. Abre a oferta e avisa o dono (modal ou WhatsApp).
        Não espera o WhatsApp: roda dentro do ciclo do supervisor."""
        runtime = self._slots.for_id(slot_id)
        if runtime is None:
            return
        owner = runtime.record.email
        token = secrets.token_urlsafe(24)
        now = self._clock()
        panel = self._panel_open(owner)
        offer = Offer(
            slot_id=slot_id,
            owner=owner,
            token_hash=_sha(token),
            created_at=now,
            expires_at=now + OFFER_SECONDS,
            via=VIA_PANEL if panel else VIA_SENDING,
        )
        self._offers[slot_id] = offer
        logger.info("slot %s taken over; offer via %s", slot_id, offer.via)
        if panel:
            await runtime.store.add_event(
                "assumido",
                "Você assumiu o jogo em outro aparelho. Aviso no painel: 'Keeper assume' vale "
                f"até {_hhmm(offer.expires_at)}",
            )
        else:
            self._spawn(self._notify(offer, token, runtime))

    def view(self, email: str) -> dict[str, Any] | None:
        """A oferta aberta para o painel do dono, ou None."""
        offer = self._offer_for(email)
        if offer is None:
            return None
        return {
            "expires_at": _iso(offer.expires_at),
            "left": round(offer.expires_at - self._clock(), 1),
        }

    def resolve(self, email: str, token: str) -> Offer:
        digest = _sha(token)
        offer = next(
            (o for o in self._offers.values() if hmac.compare_digest(o.token_hash, digest)), None
        )
        if offer is None or not self._valid(offer):
            raise TakeoverError("oferta_expirada")
        if offer.owner != email:
            raise TakeoverError("outra_conta")
        return offer

    async def retake(self, email: str, token: str | None = None) -> SlotRuntime:
        """O dono mandou o keeper assumir: religa a chave (o login a seguir derruba o outro
        aparelho, e é isso que o dono pediu)."""
        offer = self.resolve(email, token) if token is not None else self._offer_for(email)
        if offer is None:
            raise TakeoverError("oferta_expirada")
        runtime = self._slots.for_id(offer.slot_id)
        if runtime is None or runtime.record.email != email:
            raise TakeoverError("oferta_expirada")
        self._offers.pop(offer.slot_id, None)
        await runtime.store.add_event("ligado", "Keeper assume de novo: pedido pelo dono")
        await runtime.supervisor.set_power(True)
        logger.info("slot %s retaken by the owner", offer.slot_id)
        return runtime

    async def dismiss(self, email: str) -> None:
        """'Estou jogando': fecha o aviso antes dos 5 min (o link do WhatsApp deixa de valer)."""
        offer = self._offer_for(email)
        if offer is None:
            return
        self._offers.pop(offer.slot_id, None)
        runtime = self._slots.for_id(offer.slot_id)
        if runtime is not None:
            await runtime.store.add_event(
                "assumido", "Você confirmou que está jogando: o keeper fica desligado"
            )

    def clear(self, email: str) -> None:
        """A chave foi ligada por outro caminho: a oferta não serve mais."""
        offer = self._offer_for(email)
        if offer is not None:
            self._offers.pop(offer.slot_id, None)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def drain(self) -> None:
        """Espera os avisos em andamento (testes)."""
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # --- internos ---------------------------------------------------------------------------

    def _valid(self, offer: Offer) -> bool:
        if offer.expires_at <= self._clock():
            self._offers.pop(offer.slot_id, None)
            return False
        runtime = self._slots.for_id(offer.slot_id)
        # Dono mudou (vaga apagada e recriada) ou a chave já foi ligada: a oferta acabou.
        return (
            runtime is not None
            and runtime.record.email == offer.owner
            and not runtime.store.power.desired_on
        )

    def _offer_for(self, email: str) -> Offer | None:
        runtime = self._slots.for_email(email)
        if runtime is None:
            return None
        offer = self._offers.get(runtime.record.id)
        if offer is None or offer.owner != email or not self._valid(offer):
            return None
        return offer

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro, name="takeover-notify")
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("takeover notification crashed: %r", task.exception())

    async def _notify(self, offer: Offer, token: str, runtime: SlotRuntime) -> None:
        phone = self._settings.phone_for(offer.owner)
        hunter = runtime.supervisor.status().game_username
        account = f" (conta {hunter})" if hunter else ""
        link = f"{self._settings.public_origin}/retomar/{token}"
        text = (
            "*Leveling Keeper*\n"
            f"Você assumiu o controle do jogo{account} e o keeper saiu para não derrubar você.\n\n"
            "Se não era sua intenção, abra este link para o keeper assumir de novo:\n"
            f"{link}\n\n"
            "Se sua intenção é jogar, ignore esta mensagem: o link expira às "
            f"{_hhmm(offer.expires_at)}."
        )
        result = await self._messenger.send(phone, text) if phone else SendResult.NO_PHONE
        current = self._offers.get(offer.slot_id)
        if current is not None and current.token_hash == offer.token_hash:
            self._offers[offer.slot_id] = replace(current, via=result.value)
        how = {
            SendResult.SENT: "link 'keeper assume' enviado no WhatsApp",
            SendResult.FAILED: "o WhatsApp falhou; o aviso fica no painel",
            SendResult.NO_PHONE: "sem WhatsApp cadastrado; o aviso fica no painel",
            SendResult.NOT_CONFIGURED: "WhatsApp não configurado; o aviso fica no painel",
        }[result]
        if self._slots.for_id(offer.slot_id) is runtime:
            await runtime.store.add_event(
                "assumido",
                f"Você assumiu o jogo em outro aparelho: {how} (vale até "
                f"{_hhmm(offer.expires_at)})",
            )
