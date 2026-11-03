"""Slots: uma instância do jogo por jogador liberado, no máximo `max_slots` ao mesmo tempo.

Cada slot tem pasta própria em /data/slots/<id>/ (perfil do Chromium, state.json e credenciais
cifradas), tela própria (Xvfb + x11vnc) e supervisor próprio. O slot de uma requisição sai da
sessão Google (e-mail -> slot), nunca de um id vindo do navegador: um jogador não alcança a
instância do outro.

O teto de 2 é do próprio jogo: acima de 2 contas pelo mesmo IP, ele derruba todas.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import Secrets, Settings, read_secrets
from app.display import Display
from app.game import Driver
from app.state import StateStore, utcnow_iso, write_json_atomic
from app.supervisor import Supervisor

logger = logging.getLogger(__name__)

SOURCE_PANEL = "painel"  # usuário e senha cadastrados pelo próprio jogador no painel
SOURCE_SERVER = "servidor"  # dono sem cadastro: usuário e senha do arquivo de segredos
SOURCE_NONE = "nenhuma"


class SlotFullError(Exception):
    """Todas as vagas ocupadas."""


class DataKeyError(Exception):
    """Sem DATA_KEY válida no arquivo de segredos: não dá para guardar senha cifrada."""


@dataclass(frozen=True, slots=True)
class SlotRecord:
    id: int
    email: str
    created_at: str


# --- registro ---------------------------------------------------------------------------------


class SlotRegistry:
    """slots.json: quem ocupa cada vaga. Métodos síncronos (rodar com asyncio.to_thread)."""

    def __init__(self, data_dir: Path, max_slots: int) -> None:
        self._data_dir = data_dir
        self._path = data_dir / "slots.json"
        self._max = max_slots
        self._records: dict[int, SlotRecord] = {}

    def slot_dir(self, slot_id: int) -> Path:
        return self._data_dir / "slots" / str(slot_id)

    def records(self) -> list[SlotRecord]:
        return sorted(self._records.values(), key=lambda r: r.id)

    def by_email(self, email: str) -> SlotRecord | None:
        return next((r for r in self._records.values() if r.email == email), None)

    def load(self, owner_email: str | None) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._migrate_legacy(owner_email)
            return
        for item in raw.get("slots", []):
            record = SlotRecord(int(item["id"]), str(item["email"]), str(item["created_at"]))
            self._records[record.id] = record

    def _migrate_legacy(self, owner_email: str | None) -> None:
        """Antes dos slots havia uma instância só, em /data/state.json + /data/profile: ela vira o
        slot 1 do dono, com estado, eventos e token do jogo preservados."""
        legacy_state = self._data_dir / "state.json"
        legacy_profile = self._data_dir / "profile"
        if owner_email and (legacy_state.exists() or legacy_profile.exists()):
            target = self.slot_dir(1)
            target.mkdir(parents=True, exist_ok=True)
            if legacy_state.exists():
                os.replace(legacy_state, target / "state.json")
            if legacy_profile.exists() and not (target / "profile").exists():
                os.replace(legacy_profile, target / "profile")
            self._records[1] = SlotRecord(1, owner_email, utcnow_iso())
            logger.info("legacy instance migrated to slot 1")
        self.save()

    def add(self, email: str) -> SlotRecord:
        existing = self.by_email(email)
        if existing is not None:
            return existing
        free = [i for i in range(1, self._max + 1) if i not in self._records]
        if not free:
            raise SlotFullError
        record = SlotRecord(free[0], email, utcnow_iso())
        self._records[record.id] = record
        self.slot_dir(record.id).mkdir(parents=True, exist_ok=True)
        self.save()
        return record

    def remove(self, slot_id: int) -> None:
        self._records.pop(slot_id, None)
        self.save()
        shutil.rmtree(self.slot_dir(slot_id), ignore_errors=True)

    def save(self) -> None:
        payload = {"slots": [asdict(r) for r in self.records()]}
        write_json_atomic(self._path, payload)


# --- credenciais ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredLogin:
    username: str
    password: str = field(repr=False)
    version: float


class CredentialStore:
    """credentials.json por slot: usuário em claro, senha cifrada com Fernet (DATA_KEY).
    A chave mora no arquivo de segredos da hel1, não no volume. Métodos síncronos."""

    def __init__(self, registry: SlotRegistry) -> None:
        self._registry = registry

    def _path(self, slot_id: int) -> Path:
        return self._registry.slot_dir(slot_id) / "credentials.json"

    def save(self, slot_id: int, username: str, password: str, data_key: str) -> None:
        token = _fernet(data_key).encrypt(password.encode()).decode()
        payload = {"username": username, "password": token, "updated_at": utcnow_iso()}
        write_json_atomic(self._path(slot_id), payload)

    def username(self, slot_id: int) -> str | None:
        raw = self._raw(slot_id)
        return str(raw["username"]) if raw and raw.get("username") else None

    def read(self, slot_id: int, data_key: str) -> StoredLogin | None:
        path = self._path(slot_id)
        raw = self._raw(slot_id)
        if raw is None:
            return None
        try:
            password = _fernet(data_key).decrypt(str(raw["password"]).encode()).decode()
            version = path.stat().st_mtime
        except (DataKeyError, InvalidToken, KeyError, OSError) as exc:
            # Chave trocada ou arquivo mexido: trata como sem cadastro (o painel pede de novo).
            logger.warning("slot %s credentials unreadable: %s", slot_id, type(exc).__name__)
            return None
        return StoredLogin(str(raw.get("username", "")), password, version)

    def _raw(self, slot_id: int) -> dict[str, Any] | None:
        try:
            data = json.loads(self._path(slot_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.warning("slot %s credentials file unreadable: %s", slot_id, type(exc).__name__)
            return None
        return data if isinstance(data, dict) else None


def _fernet(data_key: str) -> Fernet:
    if not data_key:
        raise DataKeyError
    try:
        return Fernet(data_key.encode())
    except ValueError as exc:
        raise DataKeyError from exc


def credentials_provider(
    settings: Settings, creds: CredentialStore, record: SlotRecord
) -> Callable[[], Awaitable[Secrets]]:
    """O que o supervisor do slot usa para entrar no jogo: o cadastro do painel; sem ele, o dono
    usa o usuário/senha do arquivo de segredos (o jeito de antes dos slots)."""

    def load() -> Secrets:
        secrets = read_secrets(settings.secrets_file)
        stored = creds.read(record.id, secrets.data_key)
        if stored is not None:
            return Secrets(
                game_username=stored.username,
                game_password=stored.password,
                version=stored.version,
                source=SOURCE_PANEL,
            )
        if record.email in settings.admin_email_set and secrets.has_game_credentials:
            return Secrets(
                game_username=secrets.game_username,
                game_password=secrets.game_password,
                version=secrets.version,
                source=SOURCE_SERVER,
            )
        return Secrets(source=SOURCE_NONE)

    async def provide() -> Secrets:
        return await asyncio.to_thread(load)

    return provide


# --- instâncias em execução -------------------------------------------------------------------


@dataclass
class SlotRuntime:
    record: SlotRecord
    store: StateStore
    supervisor: Supervisor
    driver: Driver
    vnc_port: int
    display: Display | None = None
    task: asyncio.Task[None] | None = None
    last_screenshot: float = 0.0

    def healthy(self) -> bool:
        task_ok = self.task is None or not self.task.done()
        display_ok = self.display is None or self.display.alive()
        return task_ok and display_ok

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        # Fecha o Chromium sem mexer na chave: depois de um redeploy ele volta como estava.
        await self.driver.close()
        if self.display is not None:
            await self.display.stop()


RuntimeFactory = Callable[[SlotRecord], Awaitable[SlotRuntime]]


class SlotManager:
    def __init__(
        self,
        settings: Settings,
        registry: SlotRegistry,
        creds: CredentialStore,
        factory: RuntimeFactory,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._creds = creds
        self._factory = factory
        self._runtimes: dict[int, SlotRuntime] = {}
        self._lock = asyncio.Lock()

    @property
    def max_slots(self) -> int:
        return self._settings.max_slots

    @property
    def used(self) -> int:
        return len(self._runtimes)

    async def start(self) -> None:
        owner = self._settings.admin_email_list[0] if self._settings.admin_email_list else None
        await asyncio.to_thread(self._registry.load, owner)
        for record in self._registry.records():
            if record.email not in self._settings.allowed_email_set:
                # E-mail saiu da lista: a vaga fica guardada, mas a instância não roda.
                logger.warning("slot %s belongs to a non-allowed account; not started", record.id)
                continue
            self._runtimes[record.id] = await self._factory(record)

    def for_email(self, email: str) -> SlotRuntime | None:
        record = self._registry.by_email(email)
        return None if record is None else self._runtimes.get(record.id)

    def capacity_left(self) -> int:
        return self._settings.max_slots - len(self._registry.records())

    async def save_credentials(self, email: str, username: str, password: str) -> SlotRuntime:
        """Cria a instância do jogador se ainda não existe e grava usuário + senha cifrada."""
        secrets = await asyncio.to_thread(read_secrets, self._settings.secrets_file)
        _fernet(secrets.data_key)  # sem chave válida, nada é criado
        async with self._lock:
            record = self._registry.by_email(email)
            if record is None:
                record = await asyncio.to_thread(self._registry.add, email)
            await asyncio.to_thread(
                self._creds.save, record.id, username, password, secrets.data_key
            )
            runtime = self._runtimes.get(record.id)
            if runtime is None:
                runtime = await self._factory(record)
                self._runtimes[record.id] = runtime
                await runtime.store.add_event("criada", "Instância criada")
        await runtime.store.add_event("login", f"Credenciais do jogo atualizadas ({username})")
        await runtime.supervisor.credentials_changed()
        return runtime

    async def delete(self, email: str) -> bool:
        async with self._lock:
            record = self._registry.by_email(email)
            if record is None:
                return False
            runtime = self._runtimes.pop(record.id, None)
            if runtime is not None:
                await runtime.stop()
            await asyncio.to_thread(self._registry.remove, record.id)
        logger.info("slot %s deleted", record.id)
        return True

    async def credential_info(self, runtime: SlotRuntime) -> tuple[str | None, str]:
        """(nome de caçador, origem) para o painel. Nunca a senha."""
        username = await asyncio.to_thread(self._creds.username, runtime.record.id)
        if username:
            return username, SOURCE_PANEL
        status = runtime.supervisor.status()
        if status.has_credentials:
            return status.game_username or None, SOURCE_SERVER
        return None, SOURCE_NONE

    def healthy(self) -> bool:
        return all(r.healthy() for r in self._runtimes.values())

    async def stop_all(self) -> None:
        for runtime in list(self._runtimes.values()):
            await runtime.stop()
        self._runtimes.clear()
