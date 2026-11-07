"""Slots: uma instância do jogo por jogador liberado, no máximo `max_slots` ao mesmo tempo.

Cada slot tem pasta própria em /data/slots/<id>/ (perfil do Chromium, state.json e as contas do
jogo cifradas), tela própria (Xvfb + X0tigervnc) e supervisor próprio. O slot de uma requisição sai
da sessão Google (e-mail -> slot), nunca de um id vindo do navegador: um jogador não alcança a
instância do outro.

O jogo derruba todas as contas acima de 2 pelo mesmo IP: os slots acima de DIRECT_SLOTS saem
por outro IP (app/egress.py). Uma conta do jogo só fica ativa em uma vaga: duas vagas na mesma
conta se derrubariam ("acessada em outro dispositivo").
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.config import Secrets, Settings, read_secrets
from app.display import Display
from app.game import Driver
from app.state import StateStore, utcnow_iso, write_json_atomic
from app.supervisor import Supervisor

logger = logging.getLogger(__name__)

SOURCE_PANEL = "painel"  # conta cadastrada pelo próprio jogador no painel
SOURCE_SERVER = "servidor"  # conta do dono no arquivo de segredos
SOURCE_NONE = "nenhuma"

# A conta do arquivo de segredos não mora no accounts.json; no painel ela tem este id fixo.
SERVER_ACCOUNT_ID = "servidor"
ACCOUNT_ID_PATTERN = r"^(?:[0-9a-f]{12}|servidor)$"
MAX_ACCOUNTS = 20  # por jogador


class SlotError(Exception):
    """Recusa com código estável; a API traduz o código para status HTTP."""

    code = "erro"


class SlotFullError(SlotError):
    """Todas as vagas ocupadas."""

    code = "sem_vaga"


class DataKeyError(SlotError):
    """Sem DATA_KEY válida no arquivo de segredos: não dá para guardar senha cifrada."""

    code = "sem_chave"


class NoInstanceError(SlotError):
    code = "sem_instancia"


class AccountLimitError(SlotError):
    code = "contas_demais"


class AccountNotFoundError(SlotError):
    code = "conta_nao_encontrada"


class AccountInUseError(SlotError):
    """A conta já está ativa na outra vaga."""

    code = "conta_em_uso"


class AccountActiveError(SlotError):
    """A conta em uso não sai da lista: troca antes."""

    code = "conta_ativa"


class ServerAccountError(SlotError):
    """A conta do arquivo de segredos só muda no arquivo."""

    code = "conta_do_servidor"


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


# --- contas do jogo ---------------------------------------------------------------------------


def account_key(username: str) -> str:
    """Mesma conta, grafias diferentes ("Mear" e "mear"): compara sem caixa."""
    return username.casefold()


@dataclass(frozen=True, slots=True)
class GameAccount:
    id: str
    username: str
    created_at: str
    # Muda a cada senha nova: o supervisor destrava o login quando ela muda.
    rev: float
    token: str = field(repr=False)  # senha cifrada (Fernet)


@dataclass(frozen=True, slots=True)
class AccountBook:
    """As contas de um slot e a escolhida. `active` None = nenhuma escolha gravada."""

    active: str | None = None
    accounts: tuple[GameAccount, ...] = ()

    def get(self, account_id: str) -> GameAccount | None:
        return next((a for a in self.accounts if a.id == account_id), None)

    def find(self, username: str) -> GameAccount | None:
        key = account_key(username)
        return next((a for a in self.accounts if account_key(a.username) == key), None)


@dataclass(frozen=True, slots=True)
class ActiveAccount:
    id: str
    username: str
    source: str  # SOURCE_PANEL ou SOURCE_SERVER


@dataclass(frozen=True, slots=True)
class AccountView:
    """O que o painel vê de cada conta. Nunca a senha."""

    id: str
    username: str
    source: str
    active: bool


def resolve_active(book: AccountBook, server_user: str | None) -> ActiveAccount | None:
    """Conta que o slot usa agora. Sem escolha gravada, o dono fica na do arquivo de segredos
    (o jeito de antes das contas)."""
    if book.active is None or book.active == SERVER_ACCOUNT_ID:
        if server_user:
            return ActiveAccount(SERVER_ACCOUNT_ID, server_user, SOURCE_SERVER)
        return None
    account = book.get(book.active)
    return None if account is None else ActiveAccount(account.id, account.username, SOURCE_PANEL)


def server_username(settings: Settings, record: SlotRecord, secrets: Secrets) -> str | None:
    """A conta do arquivo de segredos vale só para o dono."""
    if record.email in settings.admin_email_set and secrets.has_game_credentials:
        return secrets.game_username
    return None


class AccountStore:
    """accounts.json por slot: as contas do jogo do jogador e qual está ativa. Usuário em claro,
    senha cifrada com Fernet (DATA_KEY); a chave mora no arquivo de segredos da hel1, não no
    volume. Métodos síncronos (rodar com asyncio.to_thread). Quem grava é o SlotManager, sob o
    lock dele; o supervisor só lê, e a gravação atômica não deixa ele ver arquivo pela metade."""

    def __init__(self, registry: SlotRegistry) -> None:
        self._registry = registry

    def _path(self, slot_id: int) -> Path:
        return self._registry.slot_dir(slot_id) / "accounts.json"

    def _legacy_path(self, slot_id: int) -> Path:
        return self._registry.slot_dir(slot_id) / "credentials.json"

    def load(self, slot_id: int) -> AccountBook:
        try:
            raw = json.loads(self._path(slot_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return AccountBook()
        except (OSError, ValueError) as exc:
            logger.warning("slot %s accounts file unreadable: %s", slot_id, type(exc).__name__)
            return AccountBook()
        try:
            accounts = tuple(
                GameAccount(
                    id=str(item["id"]),
                    username=str(item["username"]),
                    created_at=str(item["created_at"]),
                    rev=float(item["rev"]),
                    token=str(item["password"]),
                )
                for item in raw["accounts"]
            )
            active = raw.get("active")
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("slot %s accounts file malformed: %s", slot_id, type(exc).__name__)
            return AccountBook()
        return AccountBook(str(active) if active else None, accounts)

    def upsert(
        self, slot_id: int, username: str, password: str, data_key: str
    ) -> tuple[GameAccount, bool]:
        """Conta nova, ou senha nova para um caçador que já está na lista. (conta, criou?)"""
        token = _fernet(data_key).encrypt(password.encode()).decode()
        book = self.load(slot_id)
        existing = book.find(username)
        if existing is not None:
            updated = replace(existing, username=username, rev=time.time(), token=token)
            accounts = tuple(updated if a.id == existing.id else a for a in book.accounts)
            self._save(slot_id, replace(book, accounts=accounts))
            return updated, False
        if len(book.accounts) >= MAX_ACCOUNTS:
            raise AccountLimitError
        account = GameAccount(os.urandom(6).hex(), username, utcnow_iso(), time.time(), token)
        self._save(slot_id, replace(book, accounts=(*book.accounts, account)))
        return account, True

    def remove(self, slot_id: int, account_id: str) -> None:
        book = self.load(slot_id)
        accounts = tuple(a for a in book.accounts if a.id != account_id)
        self._save(slot_id, replace(book, accounts=accounts))

    def set_active(self, slot_id: int, account_id: str) -> None:
        self._save(slot_id, replace(self.load(slot_id), active=account_id))

    def _save(self, slot_id: int, book: AccountBook) -> None:
        payload = {
            "active": book.active,
            "accounts": [
                {
                    "id": a.id,
                    "username": a.username,
                    "password": a.token,
                    "created_at": a.created_at,
                    "rev": a.rev,
                }
                for a in book.accounts
            ],
        }
        write_json_atomic(self._path(slot_id), payload)

    def migrate_legacy(self, slot_id: int) -> None:
        """credentials.json (um caçador por slot, antes das contas) vira a conta ativa."""
        legacy = self._legacy_path(slot_id)
        if self._path(slot_id).exists() or not legacy.exists():
            return
        try:
            raw = json.loads(legacy.read_text(encoding="utf-8"))
            username, token = str(raw["username"]), str(raw["password"])
            rev = legacy.stat().st_mtime
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("slot %s legacy credentials unreadable: %s", slot_id, type(exc).__name__)
            return
        account = GameAccount(
            os.urandom(6).hex(), username, str(raw.get("updated_at") or utcnow_iso()), rev, token
        )
        self._save(slot_id, AccountBook(account.id, (account,)))
        legacy.unlink()
        logger.info("slot %s legacy credentials migrated to accounts.json", slot_id)


def decrypt_password(account: GameAccount, data_key: str) -> str | None:
    try:
        return _fernet(data_key).decrypt(account.token.encode()).decode()
    except (DataKeyError, InvalidToken) as exc:
        # Chave trocada ou arquivo mexido: o painel mostra a conta, mas o robô não entra com ela.
        logger.warning("account %s unreadable: %s", account.id, type(exc).__name__)
        return None


def _fernet(data_key: str) -> Fernet:
    if not data_key:
        raise DataKeyError
    try:
        return Fernet(data_key.encode())
    except ValueError as exc:
        raise DataKeyError from exc


def credentials_provider(
    settings: Settings, accounts: AccountStore, record: SlotRecord
) -> Callable[[], Awaitable[Secrets]]:
    """O que o supervisor do slot usa para entrar no jogo: a conta ativa (do painel ou, para o
    dono, a do arquivo de segredos). Lido a cada ciclo: trocar de conta não pede reinício."""

    def load() -> Secrets:
        secrets = read_secrets(settings.secrets_file)
        book = accounts.load(record.id)
        active = resolve_active(book, server_username(settings, record, secrets))
        if active is None:
            return Secrets(source=SOURCE_NONE)
        if active.source == SOURCE_SERVER:
            return Secrets(
                game_username=secrets.game_username,
                game_password=secrets.game_password,
                version=secrets.version,
                source=SOURCE_SERVER,
            )
        account = book.get(active.id)
        password = None if account is None else decrypt_password(account, secrets.data_key)
        if account is None or password is None:
            return Secrets(source=SOURCE_NONE)
        return Secrets(
            game_username=account.username,
            game_password=password,
            version=account.rev,
            source=SOURCE_PANEL,
        )

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
        accounts: AccountStore,
        factory: RuntimeFactory,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._accounts = accounts
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
            await asyncio.to_thread(self._accounts.migrate_legacy, record.id)
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

    # --- contas (todas as gravações sob self._lock) -----------------------------------------

    async def save_account(self, email: str, username: str, password: str) -> SlotRuntime:
        """Cadastra uma conta (ou a senha nova de uma que já está na lista). Sem instância, cria
        a instância com essa conta ativa. Sem conta ativa, a nova vira a ativa."""
        secrets = await asyncio.to_thread(read_secrets, self._settings.secrets_file)
        _fernet(secrets.data_key)  # sem chave válida, nada é criado
        async with self._lock:
            record = self._registry.by_email(email)
            new_slot = record is None
            if record is None:
                # Recusa antes de ocupar a vaga: uma instância sem conta usável não serve.
                if await self._active_elsewhere(username, None, secrets):
                    raise AccountInUseError
                record = await asyncio.to_thread(self._registry.add, email)
            account, created = await asyncio.to_thread(
                self._accounts.upsert, record.id, username, password, secrets.data_key
            )
            book = await asyncio.to_thread(self._accounts.load, record.id)
            current = resolve_active(book, server_username(self._settings, record, secrets))
            activate = (new_slot or current is None) and not await self._active_elsewhere(
                account.username, record.id, secrets
            )
            if activate:
                await asyncio.to_thread(self._accounts.set_active, record.id, account.id)
            runtime = self._runtimes.get(record.id)
            if runtime is None:
                runtime = await self._factory(record)
                self._runtimes[record.id] = runtime
                await runtime.store.add_event("criada", "Instância criada")
        what = "cadastrada" if created else "com senha nova"
        suffix = " e ativa" if activate else ""
        await runtime.store.add_event("conta", f"Conta {account.username} {what}{suffix}")
        await runtime.supervisor.credentials_changed()
        return runtime

    async def activate_account(self, email: str, account_id: str) -> SlotRuntime:
        """Troca a conta da instância. Fecha o navegador e apaga o perfil (o token da conta
        anterior mora nele); a chave fica como estava, então ligada reabre já na conta nova."""
        secrets = await asyncio.to_thread(read_secrets, self._settings.secrets_file)
        async with self._lock:
            record, runtime = self._owned(email)
            book = await asyncio.to_thread(self._accounts.load, record.id)
            server = server_username(self._settings, record, secrets)
            target = _target(book, server, account_id)
            current = resolve_active(book, server)
            if current is not None and current.id == target.id:
                return runtime
            if await self._active_elsewhere(target.username, record.id, secrets):
                raise AccountInUseError
            try:
                async with runtime.supervisor.hold():
                    await runtime.driver.forget_session()
                    await asyncio.to_thread(self._accounts.set_active, record.id, target.id)
                    await runtime.store.add_event("conta", f"Conta ativa agora: {target.username}")
            except OSError as exc:
                logger.warning("slot %s account switch failed: %s", record.id, type(exc).__name__)
                await runtime.store.add_event(
                    "erro", f"Troca para {target.username} falhou: a conta anterior continua"
                )
                raise
        logger.info("slot %s switched account", record.id)
        return runtime

    async def remove_account(self, email: str, account_id: str) -> SlotRuntime:
        if account_id == SERVER_ACCOUNT_ID:
            raise ServerAccountError
        secrets = await asyncio.to_thread(read_secrets, self._settings.secrets_file)
        async with self._lock:
            record, runtime = self._owned(email)
            book = await asyncio.to_thread(self._accounts.load, record.id)
            account = book.get(account_id)
            if account is None:
                raise AccountNotFoundError
            current = resolve_active(book, server_username(self._settings, record, secrets))
            if current is not None and current.id == account.id:
                raise AccountActiveError
            await asyncio.to_thread(self._accounts.remove, record.id, account.id)
        await runtime.store.add_event("conta", f"Conta {account.username} removida")
        return runtime

    async def accounts_view(self, runtime: SlotRuntime) -> list[AccountView]:
        """Contas para o painel: a do arquivo de segredos (só o dono) primeiro."""
        record = runtime.record

        def build() -> list[AccountView]:
            secrets = read_secrets(self._settings.secrets_file)
            book = self._accounts.load(record.id)
            server = server_username(self._settings, record, secrets)
            active = resolve_active(book, server)
            active_id = None if active is None else active.id
            views = [
                AccountView(a.id, a.username, SOURCE_PANEL, a.id == active_id)
                for a in book.accounts
            ]
            if server:
                own = AccountView(
                    SERVER_ACCOUNT_ID, server, SOURCE_SERVER, active_id == SERVER_ACCOUNT_ID
                )
                views.insert(0, own)
            return views

        return await asyncio.to_thread(build)

    def _owned(self, email: str) -> tuple[SlotRecord, SlotRuntime]:
        record = self._registry.by_email(email)
        runtime = None if record is None else self._runtimes.get(record.id)
        if record is None or runtime is None:
            raise NoInstanceError
        return record, runtime

    async def _active_elsewhere(self, username: str, slot_id: int | None, secrets: Secrets) -> bool:
        """A conta já é a ativa de outra vaga? Conta toda vaga registrada, rodando ou não: a que
        está parada volta com a mesma conta quando o e-mail for liberado de novo."""
        key = account_key(username)
        for other in self._registry.records():
            if other.id == slot_id:
                continue
            book = await asyncio.to_thread(self._accounts.load, other.id)
            active = resolve_active(book, server_username(self._settings, other, secrets))
            if active is not None and account_key(active.username) == key:
                return True
        return False

    # --- vaga --------------------------------------------------------------------------------

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

    def healthy(self) -> bool:
        return all(r.healthy() for r in self._runtimes.values())

    async def stop_all(self) -> None:
        for runtime in list(self._runtimes.values()):
            await runtime.stop()
        self._runtimes.clear()


def _target(book: AccountBook, server_user: str | None, account_id: str) -> ActiveAccount:
    if account_id == SERVER_ACCOUNT_ID:
        if not server_user:
            raise AccountNotFoundError
        return ActiveAccount(SERVER_ACCOUNT_ID, server_user, SOURCE_SERVER)
    account = book.get(account_id)
    if account is None:
        raise AccountNotFoundError
    return ActiveAccount(account.id, account.username, SOURCE_PANEL)
