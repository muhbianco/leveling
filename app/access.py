"""Acesso à tela remota da instância de outro jogador.

Fluxo: o admin pede acesso à instância de outro jogador → o keeper manda ao WhatsApp do dono da
instância um link `/acesso/<token>` → o dono abre o link logado com a conta Google dele e libera
por um prazo (ou recusa) → quem pediu abre `/remota?slot=N` com a própria sessão Google.

Regras (negado por padrão):
- Só admin (ADMIN_EMAILS) pede e usa acesso. Ninguém pede acesso à própria instância.
- O token do link só localiza o pedido: quem decide é a sessão do dono (o e-mail da sessão tem de
  ser o dono do slot naquele momento). No disco fica só o hash do token.
- Um pedido por par (quem pede, dono) de cada vez; o recusado segura um pedido novo até expirar,
  para o WhatsApp do dono não virar alvo de insistência. O pedido vale REQUEST_TTL_S.
- O acesso vale o prazo escolhido pelo dono (GRANT_HOURS) e cai antes se o dono revogar, se quem
  recebeu sair, se o dono apagar a instância ou se qualquer um sair da lista de e-mails.
- Revogar ou vencer derruba a tela remota que estiver aberta (watch()/grant_left()).
- O pedido aparece também no painel do dono: WhatsApp fora do ar não trava o fluxo.

Estado em /data/access.json (600). Métodos públicos rodam no laço do asyncio; as gravações vão
para thread.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from collections.abc import Callable, Coroutine
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.slots import SlotManager, SlotRuntime
from app.state import write_json_atomic
from app.whatsapp import Messenger, SendResult

logger = logging.getLogger(__name__)

REQUEST_TTL_S = 3600
GRANT_HOURS = (1, 12, 168)
MAX_REQUESTS = 64
MAX_GRANTS = 64
TOKEN_PATTERN = r"^[A-Za-z0-9_-]{20,64}$"  # noqa: S105 (formato do token do link, não um segredo)
ID_PATTERN = r"^[0-9a-f]{12}$"
# Horário de Brasília nas mensagens (sem horário de verão desde 2019). Fuso fixo para não depender
# do tzdata da máquina.
BRT = timezone(timedelta(hours=-3))

PENDING = "pendente"
DENIED = "recusado"
NOTIFY_SENDING = "enviando"


class AccessError(Exception):
    """Recusa com código estável; a API traduz para status HTTP."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class AccessRequest:
    id: str
    token_hash: str
    requester: str
    owner: str
    slot_id: int
    created_at: float
    expires_at: float
    status: str  # PENDING | DENIED
    notify: str  # NOTIFY_SENDING ou um SendResult


@dataclass(frozen=True, slots=True)
class AccessGrant:
    id: str
    requester: str
    owner: str
    slot_id: int
    granted_at: float
    expires_at: float


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def brt(ts: float, with_day: bool = False) -> str:
    return datetime.fromtimestamp(ts, BRT).strftime("%d/%m %H:%M" if with_day else "%H:%M")


class AccessService:
    def __init__(
        self,
        settings: Settings,
        slots: SlotManager,
        messenger: Messenger,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._slots = slots
        self._messenger = messenger
        self._clock = clock
        self._path = settings.data_dir / "access.json"
        self._requests: list[AccessRequest] = []
        self._grants: list[AccessGrant] = []
        self._lock = asyncio.Lock()
        self._watchers: dict[str, set[asyncio.Event]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # --- ciclo de vida --------------------------------------------------------------------------

    async def load(self) -> None:
        raw = await asyncio.to_thread(_read_json, self._path)
        try:
            requests = [AccessRequest(**item) for item in raw.get("requests", [])]
            grants = [AccessGrant(**item) for item in raw.get("grants", [])]
        except (TypeError, ValueError) as exc:
            logger.warning("access file malformed, starting empty: %s", type(exc).__name__)
            requests, grants = [], []
        # Envio interrompido por um restart: não se sabe se chegou; o dono ainda vê no painel.
        self._requests = [
            replace(r, notify=SendResult.FAILED.value) if r.notify == NOTIFY_SENDING else r
            for r in requests
        ]
        self._grants = grants
        self._prune()

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # --- pedidos ----------------------------------------------------------------------------------

    async def request(self, requester: str, slot_id: int) -> AccessRequest:
        if requester not in self._settings.admin_email_set:
            raise AccessError("nao_admin")
        runtime = self._slots.for_id(slot_id)
        if runtime is None:
            raise AccessError("slot_nao_encontrado")
        owner = runtime.record.email
        if owner == requester:
            raise AccessError("proprio_slot")
        token = secrets.token_urlsafe(24)
        async with self._lock:
            self._prune()
            if self._valid_grant(requester, slot_id) is not None:
                raise AccessError("ja_liberado")
            existing = self._pair_request(requester, owner)
            if existing is not None:
                if existing.status == DENIED:
                    raise AccessError("pedido_recusado")
                if existing.notify != SendResult.FAILED.value:
                    raise AccessError("pedido_pendente")
                # WhatsApp falhou: o pedido novo substitui o antigo (link novo, envio novo).
                self._requests.remove(existing)
            if len(self._requests) >= MAX_REQUESTS:
                raise AccessError("pedidos_demais")
            now = self._clock()
            req = AccessRequest(
                id=os.urandom(6).hex(),
                token_hash=_sha(token),
                requester=requester,
                owner=owner,
                slot_id=slot_id,
                created_at=now,
                expires_at=now + REQUEST_TTL_S,
                status=PENDING,
                notify=NOTIFY_SENDING,
            )
            self._requests.append(req)
            await self._save()
        logger.info("access requested for slot %s", slot_id)
        self._spawn(self._notify_owner(req, token, runtime))
        return req

    async def cancel(self, requester: str, request_id: str) -> None:
        """Quem pediu desiste de um pedido pendente (o recusado fica até expirar)."""
        async with self._lock:
            self._prune()
            req = self._request_by_id(request_id)
            if req is None or req.requester != requester or req.status != PENDING:
                raise AccessError("pedido_nao_encontrado")
            self._requests.remove(req)
            await self._save()

    def resolve_link(self, email: str, token: str) -> AccessRequest:
        """O pedido do link, se a sessão é do dono. O token sozinho não libera nada."""
        self._prune()
        digest = _sha(token)
        req = next((r for r in self._requests if hmac.compare_digest(r.token_hash, digest)), None)
        if req is None or req.status != PENDING:
            raise AccessError("pedido_nao_encontrado")
        if req.owner != email:
            raise AccessError("outra_conta")
        return req

    async def approve(self, owner: str, request_id: str, hours: int) -> AccessGrant:
        if hours not in GRANT_HOURS:
            raise AccessError("prazo_invalido")
        async with self._lock:
            self._prune()
            req = self._owned_pending(owner, request_id)
            runtime = self._slots.for_id(req.slot_id)
            if runtime is None or runtime.record.email != owner:
                # A instância do pedido não é mais dele (apagou e alguém pegou a vaga).
                self._requests.remove(req)
                await self._save()
                raise AccessError("pedido_nao_encontrado")
            now = self._clock()
            expires = now + hours * 3600
            current = self._pair_grant(req.requester, owner)
            if current is not None:
                # Mesmo id: a tela remota aberta continua e passa a valer o prazo novo.
                grant = replace(current, slot_id=req.slot_id, expires_at=expires)
                self._grants[self._grants.index(current)] = grant
            else:
                if len(self._grants) >= MAX_GRANTS:
                    raise AccessError("acessos_demais")
                grant = AccessGrant(
                    os.urandom(6).hex(), req.requester, owner, req.slot_id, now, expires
                )
                self._grants.append(grant)
            self._requests.remove(req)
            await self._save()
        await self._event(
            runtime,
            f"Tela remota liberada para {self.label(grant.requester)} até "
            f"{brt(expires, with_day=True)}",
        )
        logger.info("access granted on slot %s for %sh", grant.slot_id, hours)
        self._spawn(self._notify_requester(grant, runtime))
        return grant

    async def deny(self, owner: str, request_id: str) -> None:
        async with self._lock:
            self._prune()
            req = self._owned_pending(owner, request_id)
            self._requests[self._requests.index(req)] = replace(req, status=DENIED)
            await self._save()
        runtime = self._slots.for_id(req.slot_id)
        if runtime is not None and runtime.record.email == owner:
            await self._event(runtime, f"Pedido de acesso de {self.label(req.requester)} recusado")

    async def revoke(self, email: str, grant_id: str) -> None:
        """O dono revoga, ou quem recebeu abre mão. Derruba a tela remota aberta na hora."""
        async with self._lock:
            self._prune()
            grant = next((g for g in self._grants if g.id == grant_id), None)
            if grant is None or email not in (grant.owner, grant.requester):
                raise AccessError("acesso_nao_encontrado")
            self._grants.remove(grant)
            await self._save()
        self._fire(grant.id)
        runtime = self._slots.for_id(grant.slot_id)
        if runtime is not None and runtime.record.email == grant.owner:
            who = "pelo dono" if email == grant.owner else "por quem recebeu"
            await self._event(runtime, f"Acesso de {self.label(grant.requester)} encerrado {who}")

    async def forget_owner(self, owner: str) -> None:
        """A instância foi apagada: pedidos e acessos a ela acabam."""
        async with self._lock:
            gone = [g for g in self._grants if g.owner == owner]
            self._grants = [g for g in self._grants if g.owner != owner]
            self._requests = [r for r in self._requests if r.owner != owner]
            await self._save()
        for grant in gone:
            self._fire(grant.id)

    # --- tela remota ------------------------------------------------------------------------------

    def authorize_view(
        self, email: str, slot_id: int | None
    ) -> tuple[SlotRuntime, AccessGrant | None]:
        """A instância que esta sessão pode ver. Sem slot (ou o próprio): a dela. Outro slot: só
        com acesso válido."""
        own = self._slots.for_email(email)
        if slot_id is None or (own is not None and own.record.id == slot_id):
            if own is None:
                raise AccessError("sem_instancia")
            return own, None
        grant = self._valid_grant(email, slot_id)
        runtime = self._slots.for_id(slot_id)
        if grant is None or runtime is None:
            raise AccessError("acesso_negado")
        return runtime, grant

    def grant_left(self, grant_id: str) -> float | None:
        """Segundos que o acesso ainda vale; None se acabou por qualquer motivo."""
        grant = next((g for g in self._grants if g.id == grant_id), None)
        if grant is None or self._valid_grant(grant.requester, grant.slot_id) != grant:
            return None
        return grant.expires_at - self._clock()

    def watch(self, grant_id: str) -> asyncio.Event:
        event = asyncio.Event()
        self._watchers.setdefault(grant_id, set()).add(event)
        return event

    def unwatch(self, grant_id: str, event: asyncio.Event) -> None:
        events = self._watchers.get(grant_id)
        if events is not None:
            events.discard(event)
            if not events:
                del self._watchers[grant_id]

    # --- leitura para o painel ------------------------------------------------------------------

    def label(self, email: str) -> str:
        """Nome para mensagens: o caçador ativo e o e-mail, ou só o e-mail sem instância."""
        hunter = self._hunter(email)
        return f"{hunter} ({email})" if hunter else email

    def view(self, email: str) -> dict[str, Any]:
        self._prune()
        is_admin = email in self._settings.admin_email_set
        own = self._slots.for_email(email)
        incoming = [
            {
                "id": r.id,
                "name": self._hunter(r.requester),
                "email": r.requester,
                "created_at": iso(r.created_at),
                "expires_at": iso(r.expires_at),
            }
            for r in self._requests
            if r.owner == email and r.status == PENDING
        ]
        granted = [
            {
                "id": g.id,
                "name": self._hunter(g.requester),
                "email": g.requester,
                "expires_at": iso(g.expires_at),
            }
            for g in self._grants
            if g.owner == email and self._valid_grant(g.requester, g.slot_id) == g
        ]
        payload: dict[str, Any] = {
            "admin": is_admin,
            "shared": None,
            "incoming": incoming if own is not None else [],
            "granted": granted if own is not None else [],
        }
        if is_admin:
            payload["shared"] = [
                self._shared_entry(email, runtime)
                for runtime in self._slots.running()
                if runtime.record.email != email
            ]
        return payload

    def _shared_entry(self, email: str, runtime: SlotRuntime) -> dict[str, Any]:
        record = runtime.record
        status = runtime.supervisor.status()
        access: dict[str, Any] = {"state": "nenhum"}
        grant = self._valid_grant(email, record.id)
        req = self._pair_request(email, record.email)
        if grant is not None:
            access = {
                "state": "liberado",
                "grant_id": grant.id,
                "expires_at": iso(grant.expires_at),
                "remote_url": f"/remota?slot={record.id}",
            }
        elif req is not None:
            access = {
                "state": req.status,
                "request_id": req.id,
                "notify": req.notify,
                "expires_at": iso(req.expires_at),
            }
        return {
            "slot_id": record.id,
            "name": status.game_username or None,
            "email": record.email,
            "phase": status.phase,
            "desired_on": status.desired_on,
            "remote_viewers": status.remote_viewers,
            "access": access,
        }

    # --- internos ---------------------------------------------------------------------------------

    def _hunter(self, email: str) -> str | None:
        runtime = self._slots.for_email(email)
        name = "" if runtime is None else runtime.supervisor.status().game_username
        return name or None

    def _prune(self) -> None:
        now = self._clock()
        self._requests = [r for r in self._requests if r.expires_at > now]
        expired = [g for g in self._grants if g.expires_at <= now]
        if expired:
            self._grants = [g for g in self._grants if g.expires_at > now]
            for grant in expired:
                self._fire(grant.id)

    def _valid_grant(self, requester: str, slot_id: int) -> AccessGrant | None:
        """Acesso que vale agora: no prazo, os dois ainda liberados (quem recebeu ainda admin) e o
        slot ainda do mesmo dono."""
        if requester not in self._settings.admin_email_set:
            return None
        runtime = self._slots.for_id(slot_id)
        if runtime is None:
            return None
        owner = runtime.record.email
        if owner not in self._settings.allowed_email_set:
            return None
        now = self._clock()
        return next(
            (
                g
                for g in self._grants
                if g.requester == requester
                and g.slot_id == slot_id
                and g.owner == owner
                and g.expires_at > now
            ),
            None,
        )

    def _pair_request(self, requester: str, owner: str) -> AccessRequest | None:
        now = self._clock()
        return next(
            (
                r
                for r in self._requests
                if r.requester == requester and r.owner == owner and r.expires_at > now
            ),
            None,
        )

    def _pair_grant(self, requester: str, owner: str) -> AccessGrant | None:
        return next(
            (g for g in self._grants if g.requester == requester and g.owner == owner), None
        )

    def _request_by_id(self, request_id: str) -> AccessRequest | None:
        return next((r for r in self._requests if r.id == request_id), None)

    def _owned_pending(self, owner: str, request_id: str) -> AccessRequest:
        req = self._request_by_id(request_id)
        if req is None or req.status != PENDING:
            raise AccessError("pedido_nao_encontrado")
        if req.owner != owner:
            raise AccessError("outra_conta")
        return req

    async def event(self, runtime: SlotRuntime, message: str) -> None:
        """Evento no log da instância do dono, se ela ainda está no ar (uma instância apagada
        não ganha a pasta de volta por causa de um aviso atrasado)."""
        await self._event(runtime, message)

    async def _event(self, runtime: SlotRuntime, message: str) -> None:
        if self._slots.for_id(runtime.record.id) is runtime:
            await runtime.store.add_event("acesso", message)

    def _fire(self, grant_id: str) -> None:
        for event in self._watchers.get(grant_id, ()):
            event.set()

    async def _save(self) -> None:
        payload = {
            "requests": [asdict(r) for r in self._requests],
            "grants": [asdict(g) for g in self._grants],
        }
        await asyncio.to_thread(write_json_atomic, self._path, payload)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro, name="access-notify")
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("access notification crashed: %r", task.exception())

    async def _notify_owner(self, req: AccessRequest, token: str, runtime: SlotRuntime) -> None:
        phone = self._settings.phone_for(req.owner)
        link = f"{self._settings.public_origin}/acesso/{token}"
        own_hunter = self._hunter(req.owner)
        whose = f"da sua instância ({own_hunter})" if own_hunter else "da sua instância"
        text = (
            "*Leveling Keeper*\n"
            f"{self.label(req.requester)} pediu acesso à tela remota {whose}.\n\n"
            f"Para liberar ou recusar, abra com a sua conta Google do painel "
            f"(vale até {brt(req.expires_at)}):\n{link}\n\n"
            "Se não foi combinado, é só ignorar: sem resposta o pedido expira."
        )
        result = await self._messenger.send(phone, text) if phone else SendResult.NO_PHONE
        async with self._lock:
            current = self._request_by_id(req.id)
            if current is not None:
                self._requests[self._requests.index(current)] = replace(
                    current, notify=result.value
                )
                await self._save()
        how = {
            SendResult.SENT: "aviso enviado no WhatsApp",
            SendResult.FAILED: "o WhatsApp falhou",
            SendResult.NO_PHONE: "sem WhatsApp cadastrado",
            SendResult.NOT_CONFIGURED: "WhatsApp não configurado no servidor",
        }[result]
        await self._event(
            runtime,
            f"{self.label(req.requester)} pediu acesso à tela remota ({how}; "
            "o pedido também está no painel)",
        )

    async def _notify_requester(self, grant: AccessGrant, runtime: SlotRuntime) -> None:
        phone = self._settings.phone_for(grant.requester)
        if not phone:
            return
        text = (
            "*Leveling Keeper*\n"
            f"{self.label(grant.owner)} liberou a tela remota da instância até "
            f"{brt(grant.expires_at, with_day=True)}:\n"
            f"{self._settings.public_origin}/remota?slot={grant.slot_id}"
        )
        result = await self._messenger.send(phone, text)
        if result is not SendResult.SENT:
            logger.info("grant notice not delivered: %s", result.value)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("access file unreadable, starting empty: %s", type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}
