"""Acesso à tela remota da instância de outro jogador. Dois modos:

- **Pedir acesso.** O admin pede a instância de outro jogador, com tempo (DURATIONS) e motivo. O
  keeper manda ao WhatsApp do dono um convite `/acesso/<token>` que vale INVITE_TTL_S. O dono abre
  logado com a conta Google dele e aceita (vale o tempo pedido, contado a partir do aceite) ou
  recusa.
- **Abrir minha instância.** O próprio dono abre a instância dele para um jogador ou para todos
  (EVERYONE), com tempo e motivo. Quem recebe é avisado no WhatsApp.

Regras (negado por padrão):
- Só admin (ADMIN_EMAILS) vê as outras instâncias, pede e recebe acesso. Ninguém pede a própria.
- O token do convite só localiza o pedido: quem decide é a sessão do dono (o e-mail da sessão tem
  de ser o dono do slot naquele momento). No disco fica só o hash do token.
- Um pedido por par (quem pede, dono) de cada vez. O recusado segura um pedido novo até o convite
  expirar, para o WhatsApp do dono não virar alvo de insistência.
- Um acesso por (dono, destino): abrir de novo para o mesmo destino troca tempo e motivo. "Para
  todos" vale para qualquer admin, inclusive quem entrar depois.
- O acesso cai quando o prazo vence ("sempre aberto" não vence), quando o dono fecha, quando quem
  recebeu abre mão (só o acesso pessoal), quando o dono apaga a instância ou quando alguém sai da
  lista de e-mails.
- Qualquer mudança nos acessos de um slot acorda as telas remotas abertas nele (watch()), que
  reavaliam na hora: sem acesso que cubra quem está vendo, a conexão cai.
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
import math
import os
import secrets
import time
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.slots import SlotManager, SlotRuntime
from app.state import write_json_atomic
from app.whatsapp import Messenger, SendResult

logger = logging.getLogger(__name__)

# Tempo de acesso: chave da API -> segundos (None = sempre aberto, sem prazo).
DURATIONS: dict[str, int | None] = {"1h": 3600, "4h": 4 * 3600, "12h": 12 * 3600, "sempre": None}
DURATION_TEXT = {"1h": "1 hora", "4h": "4 horas", "12h": "12 horas", "sempre": "sempre aberto"}
INVITE_TTL_S = 4 * 3600
REASON_MIN = 3
REASON_MAX = 200
MAX_REQUESTS = 64
MAX_GRANTS = 64
EVERYONE = "*"
TOKEN_PATTERN = r"^[A-Za-z0-9_-]{20,64}$"  # noqa: S105 (formato do token do link, não um segredo)
ID_PATTERN = r"^[0-9a-f]{12}$"
# Horário de Brasília nas mensagens (sem horário de verão desde 2019). Fuso fixo para não depender
# do tzdata da máquina.
BRT = timezone(timedelta(hours=-3))

PENDING = "pendente"
DENIED = "recusado"
NOTIFY_SENDING = "enviando"
ORIGIN_REQUEST = "pedido"  # pedido aceito pelo dono
ORIGIN_OPEN = "aberto"  # o dono abriu por conta própria


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
    duration: str  # chave de DURATIONS: o tempo que vale depois do aceite
    reason: str
    created_at: float
    expires_at: float  # do convite, não do acesso
    status: str  # PENDING | DENIED
    notify: str  # NOTIFY_SENDING ou um SendResult


@dataclass(frozen=True, slots=True)
class AccessGrant:
    id: str
    owner: str
    slot_id: int
    audience: str  # e-mail de quem recebeu, ou EVERYONE
    origin: str  # ORIGIN_REQUEST | ORIGIN_OPEN
    duration: str
    reason: str
    granted_at: float
    expires_at: float | None  # None = sempre aberto

    def covers(self, email: str) -> bool:
        return self.audience in (email, EVERYONE)

    def live_at(self, now: float) -> bool:
        return self.expires_at is None or self.expires_at > now


def clean_reason(raw: str) -> str:
    """Motivo numa linha só, sem caractere de controle, entre REASON_MIN e REASON_MAX."""
    if any(not c.isprintable() and not c.isspace() for c in raw):
        raise ValueError("motivo com caractere inválido")
    text = " ".join(raw.split())
    if not REASON_MIN <= len(text) <= REASON_MAX:
        raise ValueError(f"motivo entre {REASON_MIN} e {REASON_MAX} caracteres")
    return text


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_or_none(ts: float | None) -> str | None:
    return None if ts is None else iso(ts)


def brt(ts: float, with_day: bool = False) -> str:
    return datetime.fromtimestamp(ts, BRT).strftime("%d/%m %H:%M" if with_day else "%H:%M")


def how_long(duration: str) -> str:
    """'por 4 horas' ou 'sem prazo (sempre aberto)', para as mensagens."""
    if duration in DURATIONS and DURATIONS[duration] is None:
        return "sem prazo (sempre aberto)"
    return f"por {DURATION_TEXT.get(duration, duration)}"


def _expiry(duration: str, start: float) -> float | None:
    seconds = DURATIONS[duration]
    return None if seconds is None else start + seconds


# --- arquivo -------------------------------------------------------------------------------------


def read_state(path: Path) -> tuple[list[AccessRequest], list[AccessGrant]]:
    """access.json, item a item: um registro estragado sai sozinho, sem levar os outros. Aceita o
    formato da primeira versão (pedido sem tempo/motivo, acesso com `requester`)."""
    raw = _read_json(path)
    requests: list[AccessRequest] = []
    for item in raw.get("requests", []):
        try:
            data = {"duration": "1h", "reason": "", **item}
            requests.append(AccessRequest(**data))
        except (TypeError, ValueError) as exc:
            logger.warning("access request dropped: %s", type(exc).__name__)
    grants: list[AccessGrant] = []
    for item in raw.get("grants", []):
        try:
            data = dict(item)
            if "requester" in data:  # primeira versão: acesso pessoal vindo de pedido
                data["audience"] = data.pop("requester")
                data.setdefault("origin", ORIGIN_REQUEST)
            data.setdefault("duration", "1h")
            data.setdefault("reason", "")
            grants.append(AccessGrant(**data))
        except (TypeError, ValueError) as exc:
            logger.warning("access grant dropped: %s", type(exc).__name__)
    return requests, grants


def write_state(
    path: Path, requests: Iterable[AccessRequest], grants: Iterable[AccessGrant]
) -> None:
    payload = {
        "requests": [asdict(r) for r in requests],
        "grants": [asdict(g) for g in grants],
    }
    write_json_atomic(path, payload)


def new_id() -> str:
    return os.urandom(6).hex()


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
        self._watchers: dict[int, set[asyncio.Event]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # --- ciclo de vida --------------------------------------------------------------------------

    async def load(self) -> None:
        requests, grants = await asyncio.to_thread(read_state, self._path)
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

    # --- modo "pedir acesso" ------------------------------------------------------------------

    async def request(
        self, requester: str, slot_id: int, duration: str, reason: str
    ) -> AccessRequest:
        if requester not in self._settings.admin_email_set:
            raise AccessError("nao_admin")
        _check_duration(duration)
        reason = _reason(reason)
        runtime = self._slots.for_id(slot_id)
        if runtime is None:
            raise AccessError("slot_nao_encontrado")
        owner = runtime.record.email
        if owner == requester:
            raise AccessError("proprio_slot")
        token = secrets.token_urlsafe(24)
        async with self._lock:
            self._prune()
            if self._best_grant(requester, slot_id) is not None:
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
                id=new_id(),
                token_hash=_sha(token),
                requester=requester,
                owner=owner,
                slot_id=slot_id,
                duration=duration,
                reason=reason,
                created_at=now,
                expires_at=now + INVITE_TTL_S,
                status=PENDING,
                notify=NOTIFY_SENDING,
            )
            self._requests.append(req)
            await self._save()
        logger.info("access requested for slot %s (%s)", slot_id, duration)
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
        """O pedido do convite, se a sessão é do dono. O token sozinho não libera nada."""
        self._prune()
        digest = _sha(token)
        req = next((r for r in self._requests if hmac.compare_digest(r.token_hash, digest)), None)
        if req is None or req.status != PENDING:
            raise AccessError("pedido_nao_encontrado")
        if req.owner != email:
            raise AccessError("outra_conta")
        return req

    async def approve(self, owner: str, request_id: str) -> AccessGrant:
        """Aceita com o tempo pedido, contado a partir de agora."""
        async with self._lock:
            self._prune()
            req = self._owned_pending(owner, request_id)
            runtime = self._slots.for_id(req.slot_id)
            if runtime is None or runtime.record.email != owner:
                # A instância do pedido não é mais dele (apagou e alguém pegou a vaga).
                self._requests.remove(req)
                await self._save()
                raise AccessError("pedido_nao_encontrado")
            grant = self._put_grant(
                owner, req.slot_id, req.requester, ORIGIN_REQUEST, req.duration, req.reason
            )
            self._requests.remove(req)
            await self._save()
        self._fire(grant.slot_id)
        await self._event(
            runtime,
            f"Pedido de {self.label(req.requester)} aceito: tela remota liberada "
            f"{self._until(grant)} (motivo: {grant.reason})",
        )
        logger.info("access granted on slot %s (%s)", grant.slot_id, grant.duration)
        self._spawn(self._notify_recipients(grant, [req.requester]))
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

    # --- modo "abrir minha instância" ---------------------------------------------------------

    async def open(self, owner: str, audience: str, duration: str, reason: str) -> AccessGrant:
        """O dono abre a própria instância para um jogador (e-mail de admin) ou para todos."""
        _check_duration(duration)
        reason = _reason(reason)
        runtime = self._slots.for_email(owner)
        if runtime is None:
            raise AccessError("sem_instancia")
        audience = audience.strip().lower()
        if audience != EVERYONE and (
            audience == owner or audience not in self._settings.admin_email_set
        ):
            raise AccessError("destino_invalido")
        async with self._lock:
            self._prune()
            grant = self._put_grant(
                owner, runtime.record.id, audience, ORIGIN_OPEN, duration, reason
            )
            # Quem acabou de ganhar acesso não precisa mais do pedido pendente.
            self._requests = [
                r
                for r in self._requests
                if not (r.owner == owner and r.status == PENDING and grant.covers(r.requester))
            ]
            await self._save()
        self._fire(grant.slot_id)
        await self._event(
            runtime,
            f"Instância aberta para {self._audience_label(audience)} {self._until(grant)} "
            f"(motivo: {reason})",
        )
        logger.info(
            "slot %s opened (%s, everyone=%s)", grant.slot_id, duration, audience == EVERYONE
        )
        self._spawn(self._notify_recipients(grant, self._recipients(grant)))
        return grant

    async def revoke(self, email: str, grant_id: str) -> None:
        """O dono fecha qualquer acesso da instância dele; quem recebeu abre mão só do acesso
        pessoal. Derruba na hora a tela remota de quem perdeu o acesso."""
        async with self._lock:
            self._prune()
            grant = next((g for g in self._grants if g.id == grant_id), None)
            allowed = grant is not None and (
                email == grant.owner or (grant.audience == email and grant.audience != EVERYONE)
            )
            if grant is None or not allowed:
                raise AccessError("acesso_nao_encontrado")
            self._grants.remove(grant)
            await self._save()
        self._fire(grant.slot_id)
        runtime = self._slots.for_id(grant.slot_id)
        if runtime is not None and runtime.record.email == grant.owner:
            who = "pelo dono" if email == grant.owner else "por quem recebeu"
            target = self._audience_label(grant.audience)
            await self._event(runtime, f"Acesso de {target} encerrado {who}")

    async def forget_owner(self, owner: str) -> None:
        """A instância foi apagada: pedidos e acessos a ela acabam."""
        async with self._lock:
            slots = {g.slot_id for g in self._grants if g.owner == owner}
            self._grants = [g for g in self._grants if g.owner != owner]
            self._requests = [r for r in self._requests if r.owner != owner]
            await self._save()
        for slot_id in slots:
            self._fire(slot_id)

    # --- tela remota ------------------------------------------------------------------------------

    def authorize_view(
        self, email: str, slot_id: int | None
    ) -> tuple[SlotRuntime, AccessGrant | None]:
        """A instância que esta sessão pode ver. Sem slot (ou o próprio): a dela. Outro slot: só
        com acesso válido (o de prazo mais longo, se houver mais de um)."""
        own = self._slots.for_email(email)
        if slot_id is None or (own is not None and own.record.id == slot_id):
            if own is None:
                raise AccessError("sem_instancia")
            return own, None
        self._prune()
        grant = self._best_grant(email, slot_id)
        runtime = self._slots.for_id(slot_id)
        if grant is None or runtime is None:
            raise AccessError("acesso_negado")
        return runtime, grant

    def access_left(self, email: str, slot_id: int) -> float | None:
        """Segundos de acesso que esta pessoa ainda tem ao slot (inf = sempre aberto); None se
        nenhum acesso a cobre."""
        self._prune()
        grant = self._best_grant(email, slot_id)
        if grant is None:
            return None
        return math.inf if grant.expires_at is None else grant.expires_at - self._clock()

    def watch(self, slot_id: int) -> asyncio.Event:
        """Evento disparado a cada mudança nos acessos do slot."""
        event = asyncio.Event()
        self._watchers.setdefault(slot_id, set()).add(event)
        return event

    def unwatch(self, slot_id: int, event: asyncio.Event) -> None:
        events = self._watchers.get(slot_id)
        if events is not None:
            events.discard(event)
            if not events:
                del self._watchers[slot_id]

    # --- leitura para o painel ------------------------------------------------------------------

    def label(self, email: str) -> str:
        """Nome para mensagens: o caçador ativo e o e-mail, ou só o e-mail sem instância."""
        hunter = self._hunter(email)
        return f"{hunter} ({email})" if hunter else email

    def view(self, email: str) -> dict[str, Any]:
        self._prune()
        is_admin = email in self._settings.admin_email_set
        own = self._slots.for_email(email)
        payload: dict[str, Any] = {
            "admin": is_admin,
            "shared": None,
            "incoming": [],
            "granted": [],
            "people": [],
        }
        if own is not None:
            payload["incoming"] = [
                {
                    "id": r.id,
                    "name": self._hunter(r.requester),
                    "email": r.requester,
                    "duration": r.duration,
                    "reason": r.reason,
                    "created_at": iso(r.created_at),
                    "expires_at": iso(r.expires_at),
                }
                for r in self._requests
                if r.owner == email and r.status == PENDING
            ]
            payload["granted"] = [
                {
                    "id": g.id,
                    "audience": "todos" if g.audience == EVERYONE else g.audience,
                    "name": None if g.audience == EVERYONE else self._hunter(g.audience),
                    "origin": g.origin,
                    "duration": g.duration,
                    "reason": g.reason,
                    "granted_at": iso(g.granted_at),
                    "expires_at": iso_or_none(g.expires_at),
                }
                for g in self._grants
                if g.owner == email and g.slot_id == own.record.id and self._live(g)
            ]
            payload["people"] = [
                {"email": other, "name": self._hunter(other)}
                for other in self._settings.admin_email_list
                if other != email
            ]
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
        grant = self._best_grant(email, record.id)
        req = self._pair_request(email, record.email)
        if grant is not None:
            access = {
                "state": "liberado",
                "grant_id": grant.id,
                "scope": "todos" if grant.audience == EVERYONE else "voce",
                "origin": grant.origin,
                "duration": grant.duration,
                "reason": grant.reason,
                "expires_at": iso_or_none(grant.expires_at),
                "remote_url": f"/remota?slot={record.id}",
            }
        elif req is not None:
            access = {
                "state": req.status,
                "request_id": req.id,
                "notify": req.notify,
                "duration": req.duration,
                "reason": req.reason,
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

    def _audience_label(self, audience: str) -> str:
        return "todos os jogadores" if audience == EVERYONE else self.label(audience)

    def _until(self, grant: AccessGrant) -> str:
        if grant.expires_at is None:
            return "sem prazo (sempre aberto)"
        return f"até {brt(grant.expires_at, with_day=True)}"

    def _put_grant(
        self, owner: str, slot_id: int, audience: str, origin: str, duration: str, reason: str
    ) -> AccessGrant:
        """Cria ou troca o acesso (dono, destino). Chamar com o lock."""
        now = self._clock()
        current = next(
            (g for g in self._grants if g.owner == owner and g.audience == audience), None
        )
        if current is None and len(self._grants) >= MAX_GRANTS:
            raise AccessError("acessos_demais")
        grant = AccessGrant(
            id=new_id() if current is None else current.id,
            owner=owner,
            slot_id=slot_id,
            audience=audience,
            origin=origin,
            duration=duration,
            reason=reason,
            granted_at=now,
            expires_at=_expiry(duration, now),
        )
        if current is None:
            self._grants.append(grant)
        else:
            self._grants[self._grants.index(current)] = grant
        return grant

    def _recipients(self, grant: AccessGrant) -> list[str]:
        if grant.audience != EVERYONE:
            return [grant.audience]
        return [e for e in self._settings.admin_email_list if e != grant.owner]

    def _prune(self) -> None:
        now = self._clock()
        self._requests = [r for r in self._requests if r.expires_at > now]
        expired = [g for g in self._grants if not g.live_at(now)]
        if expired:
            self._grants = [g for g in self._grants if g.live_at(now)]
            for slot_id in {g.slot_id for g in expired}:
                self._fire(slot_id)

    def _live(self, grant: AccessGrant) -> bool:
        """O acesso ainda vale: no prazo, slot ainda do mesmo dono (liberado na lista) e, se for
        pessoal, quem recebeu ainda é admin."""
        runtime = self._slots.for_id(grant.slot_id)
        if runtime is None or runtime.record.email != grant.owner:
            return False
        if grant.owner not in self._settings.allowed_email_set:
            return False
        if grant.audience != EVERYONE and grant.audience not in self._settings.admin_email_set:
            return False
        return grant.live_at(self._clock())

    def _best_grant(self, email: str, slot_id: int) -> AccessGrant | None:
        """O acesso de prazo mais longo que cobre esta pessoa no slot (nunca o próprio dono)."""
        if email not in self._settings.admin_email_set:
            return None
        found = [
            g
            for g in self._grants
            if g.slot_id == slot_id and g.owner != email and g.covers(email) and self._live(g)
        ]
        if not found:
            return None
        return max(found, key=lambda g: math.inf if g.expires_at is None else g.expires_at)

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

    def _fire(self, slot_id: int) -> None:
        for event in self._watchers.get(slot_id, ()):
            event.set()

    async def _save(self) -> None:
        await asyncio.to_thread(write_state, self._path, list(self._requests), list(self._grants))

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
            f"{self.label(req.requester)} pediu acesso à tela remota {whose} "
            f"{how_long(req.duration)}.\n"
            f"Motivo: {req.reason}\n\n"
            "Para aceitar ou recusar, abra com a sua conta Google do painel "
            f"(o convite vale até {brt(req.expires_at)}; o tempo de acesso só começa a contar "
            f"quando você aceitar):\n{link}\n\n"
            "Se não foi combinado, é só ignorar: sem resposta o convite expira."
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
            f"{self.label(req.requester)} pediu acesso à tela remota {how_long(req.duration)} "
            f"(motivo: {req.reason}; {how}; o pedido também está no painel)",
        )

    async def _notify_recipients(self, grant: AccessGrant, emails: list[str]) -> None:
        """Avisa quem recebeu o acesso (um por vez: a WuzAPI é uma sessão só)."""
        link = f"{self._settings.public_origin}/remota?slot={grant.slot_id}"
        whom = "para todos os jogadores" if grant.audience == EVERYONE else "para você"
        verb = "aceitou seu pedido e liberou" if grant.origin == ORIGIN_REQUEST else "abriu"
        text = (
            "*Leveling Keeper*\n"
            f"{self.label(grant.owner)} {verb} a tela remota da instância {whom}, "
            f"{self._until(grant)}.\n"
            f"Motivo: {grant.reason}\n\n"
            f"Para abrir, entre com a sua conta Google do painel:\n{link}"
        )
        for email in emails:
            phone = self._settings.phone_for(email)
            if not phone:
                continue
            result = await self._messenger.send(phone, text)
            if result is not SendResult.SENT:
                logger.info("access notice not delivered: %s", result.value)


def _check_duration(duration: str) -> None:
    if duration not in DURATIONS:
        raise AccessError("prazo_invalido")


def _reason(raw: str) -> str:
    try:
        return clean_reason(raw)
    except ValueError as exc:
        raise AccessError("motivo_invalido") from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("access file unreadable, starting empty: %s", type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}
