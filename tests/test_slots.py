from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.game import _remove_profile
from app.slots import (
    MAX_ACCOUNTS,
    SERVER_ACCOUNT_ID,
    SOURCE_NONE,
    SOURCE_PANEL,
    SOURCE_SERVER,
    AccountLimitError,
    AccountStore,
    SlotFullError,
    SlotManager,
    SlotRecord,
    SlotRegistry,
    SlotRuntime,
    credentials_provider,
    decrypt_password,
)
from app.state import StateStore
from app.supervisor import Supervisor
from tests.test_supervisor import FakeDriver

OWNER = "mear.mind@gmail.com"
PLAYER = "pugtimothy@gmail.com"
KEY = Fernet.generate_key().decode()


def make_settings(tmp_path: Path, secrets: str) -> Settings:
    path = tmp_path / "secrets.env"
    path.write_text(secrets, encoding="utf-8")
    return Settings(
        admin_emails=OWNER,
        player_emails=PLAYER,
        secrets_file=path,
        data_dir=tmp_path / "data",
        manage_display=False,
    )


def test_legacy_instance_becomes_owner_slot_1(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "profile" / "Default").mkdir(parents=True)
    (data / "profile" / "Default" / "Local Storage").write_text("token", encoding="utf-8")
    (data / "state.json").write_text(json.dumps({"desired_on": True}), encoding="utf-8")
    (data / "sessions.json").write_text("{}", encoding="utf-8")

    registry = SlotRegistry(data, max_slots=2)
    registry.load(OWNER)

    assert registry.records() == [registry.by_email(OWNER)]
    slot1 = registry.slot_dir(1)
    assert json.loads((slot1 / "state.json").read_text(encoding="utf-8"))["desired_on"] is True
    assert (slot1 / "profile" / "Default" / "Local Storage").read_text(encoding="utf-8") == "token"
    assert not (data / "state.json").exists()
    assert not (data / "profile").exists()
    assert (data / "sessions.json").exists()  # sessões do painel continuam globais

    again = SlotRegistry(data, max_slots=2)
    again.load(OWNER)
    assert [r.id for r in again.records()] == [1]


def test_fresh_volume_starts_empty(tmp_path: Path) -> None:
    registry = SlotRegistry(tmp_path / "data", max_slots=2)
    registry.load(OWNER)
    assert registry.records() == []
    assert (tmp_path / "data" / "slots.json").exists()


def test_lowest_free_id_and_cap(tmp_path: Path) -> None:
    registry = SlotRegistry(tmp_path / "data", max_slots=2)
    registry.load(None)
    a = registry.add("a@x.com")
    b = registry.add("b@x.com")
    assert (a.id, b.id) == (1, 2)
    assert registry.add("a@x.com") == a  # um slot por e-mail
    with pytest.raises(SlotFullError):
        registry.add("c@x.com")
    registry.remove(1)
    assert not registry.slot_dir(1).exists()
    assert registry.add("c@x.com").id == 1


def test_accounts_roundtrip_upsert_and_wrong_key(tmp_path: Path) -> None:
    registry = SlotRegistry(tmp_path / "data", max_slots=2)
    registry.load(None)
    record = registry.add(PLAYER)
    accounts = AccountStore(registry)
    first, created = accounts.upsert(record.id, "pug", "pw com espaço ", KEY)
    assert created
    second, _ = accounts.upsert(record.id, "alt", "pw2", KEY)

    book = accounts.load(record.id)
    assert [a.username for a in book.accounts] == ["pug", "alt"]
    assert book.active is None  # quem escolhe a ativa é o SlotManager
    stored = book.get(first.id)
    assert stored is not None
    assert decrypt_password(stored, KEY) == "pw com espaço "
    assert "pw com" not in repr(stored)
    assert stored.token not in repr(stored)
    assert decrypt_password(stored, Fernet.generate_key().decode()) is None
    assert decrypt_password(stored, "") is None

    # Mesmo caçador em outra caixa: senha nova, mesma conta (sem duplicar).
    again, created = accounts.upsert(record.id, "PUG", "nova", KEY)
    assert (again.id, created) == (first.id, False)
    assert again.rev > first.rev
    book = accounts.load(record.id)
    assert [a.username for a in book.accounts] == ["PUG", "alt"]
    updated = book.get(first.id)
    assert updated is not None
    assert decrypt_password(updated, KEY) == "nova"

    accounts.set_active(record.id, second.id)
    accounts.remove(record.id, first.id)
    book = accounts.load(record.id)
    assert (book.active, [a.id for a in book.accounts]) == (second.id, [second.id])
    raw = (registry.slot_dir(record.id) / "accounts.json").read_text(encoding="utf-8")
    assert "pw2" not in raw
    if sys.platform != "win32":
        mode = stat.S_IMODE((registry.slot_dir(record.id) / "accounts.json").stat().st_mode)
        assert mode == 0o600


def test_account_limit(tmp_path: Path) -> None:
    registry = SlotRegistry(tmp_path / "data", max_slots=2)
    registry.load(None)
    record = registry.add(PLAYER)
    accounts = AccountStore(registry)
    for i in range(MAX_ACCOUNTS):
        accounts.upsert(record.id, f"hunter{i}", "pw", KEY)
    with pytest.raises(AccountLimitError):
        accounts.upsert(record.id, "mais-um", "pw", KEY)
    # Trocar a senha de uma que já está na lista continua valendo no limite.
    assert accounts.upsert(record.id, "hunter0", "pw2", KEY)[1] is False


def test_legacy_credentials_become_the_active_account(tmp_path: Path) -> None:
    registry = SlotRegistry(tmp_path / "data", max_slots=2)
    registry.load(None)
    record = registry.add(PLAYER)
    token = Fernet(KEY.encode()).encrypt(b"velha").decode()
    legacy = registry.slot_dir(record.id) / "credentials.json"
    legacy.write_text(
        json.dumps({"username": "pug", "password": token, "updated_at": "2026-11-03T10:00:00Z"}),
        encoding="utf-8",
    )
    accounts = AccountStore(registry)
    accounts.migrate_legacy(record.id)

    book = accounts.load(record.id)
    assert len(book.accounts) == 1
    account = book.accounts[0]
    assert (book.active, account.username, account.created_at) == (
        account.id,
        "pug",
        "2026-11-03T10:00:00Z",
    )
    assert decrypt_password(account, KEY) == "velha"
    assert not legacy.exists()
    accounts.migrate_legacy(record.id)  # de novo não faz nada
    assert accounts.load(record.id) == book


async def test_provider_uses_the_active_account(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, f"GAME_USERNAME=owner\nGAME_PASSWORD=opw\nDATA_KEY={KEY}\n")
    registry = SlotRegistry(settings.data_dir, 2)
    registry.load(None)
    accounts = AccountStore(registry)
    owner = registry.add(OWNER)
    player = registry.add(PLAYER)

    # Sem escolha gravada, o dono fica na conta do arquivo de segredos (como antes).
    owner_creds = await credentials_provider(settings, accounts, owner)()
    assert (owner_creds.game_username, owner_creds.source) == ("owner", SOURCE_SERVER)
    player_creds = await credentials_provider(settings, accounts, player)()
    assert (player_creds.has_game_credentials, player_creds.source) == (False, SOURCE_NONE)

    # Cadastrar outra conta não troca a ativa; escolher troca.
    alt, _ = accounts.upsert(owner.id, "owner-alt", "apw", KEY)
    unchanged = await credentials_provider(settings, accounts, owner)()
    assert unchanged.game_username == "owner"
    accounts.set_active(owner.id, alt.id)
    switched = await credentials_provider(settings, accounts, owner)()
    assert (switched.game_username, switched.game_password, switched.source) == (
        "owner-alt",
        "apw",
        SOURCE_PANEL,
    )
    assert switched.version == alt.rev
    accounts.set_active(owner.id, SERVER_ACCOUNT_ID)
    back = await credentials_provider(settings, accounts, owner)()
    assert back.source == SOURCE_SERVER

    # Jogador não herda a conta do arquivo, mesmo escolhendo o id dela.
    accounts.set_active(player.id, SERVER_ACCOUNT_ID)
    assert (await credentials_provider(settings, accounts, player)()).source == SOURCE_NONE


async def test_only_the_first_admin_uses_the_server_account(tmp_path: Path) -> None:
    """Todos os jogadores admin (painel compartilhado): a conta do arquivo continua do dono."""
    path = tmp_path / "secrets.env"
    path.write_text(f"GAME_USERNAME=owner\nGAME_PASSWORD=opw\nDATA_KEY={KEY}\n", encoding="utf-8")
    settings = Settings(
        admin_emails=f"{OWNER},{PLAYER}", secrets_file=path, data_dir=tmp_path / "data"
    )
    registry = SlotRegistry(settings.data_dir, 2)
    registry.load(None)
    accounts = AccountStore(registry)
    owner = registry.add(OWNER)
    admin = registry.add(PLAYER)
    assert (await credentials_provider(settings, accounts, owner)()).source == SOURCE_SERVER
    accounts.set_active(admin.id, SERVER_ACCOUNT_ID)
    assert (await credentials_provider(settings, accounts, admin)()).source == SOURCE_NONE


async def test_manager_skips_accounts_no_longer_allowed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, f"DATA_KEY={KEY}\n")
    registry = SlotRegistry(settings.data_dir, 2)
    registry.load(None)
    registry.add("removido@x.com")
    registry.add(PLAYER)
    legacy = registry.slot_dir(2) / "credentials.json"
    legacy.write_text(json.dumps({"username": "pug", "password": "x"}), encoding="utf-8")
    accounts = AccountStore(registry)
    started: list[int] = []

    async def build(record: SlotRecord) -> SlotRuntime:
        started.append(record.id)
        store = StateStore(registry.slot_dir(record.id) / "state.json")
        driver = FakeDriver()
        sup = Supervisor(store, driver, credentials_provider(settings, accounts, record))
        return SlotRuntime(record, store, sup, driver, 5900)

    manager = SlotManager(settings, SlotRegistry(settings.data_dir, 2), accounts, build)
    await manager.start()
    assert started == [2]
    assert manager.for_email("removido@x.com") is None
    assert manager.capacity_left() == 0  # a vaga continua reservada
    # O cadastro de antes das contas vira a conta ativa já no boot, antes do supervisor ler.
    assert not legacy.exists()
    assert [a.username for a in accounts.load(2).accounts] == ["pug"]


def test_forgetting_the_profile_removes_it(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    (profile / "Default" / "Local Storage").mkdir(parents=True)
    (profile / "Default" / "Local Storage" / "token").write_text("t", encoding="utf-8")
    _remove_profile(profile)
    assert not profile.exists()
    _remove_profile(profile)  # já apagado: tudo bem


def test_slot_displays_and_ports_do_not_collide() -> None:
    s = Settings()
    assert [s.display_for(i) for i in (1, 2)] == [":99", ":100"]
    assert [s.vnc_port_for(i) for i in (1, 2)] == [5900, 5901]
    assert s.max_slots == 2


def test_slots_above_direct_need_the_egress_proxy() -> None:
    with pytest.raises(ValueError, match="EGRESS_PROXY"):
        Settings(max_slots=3)
    with pytest.raises(ValueError, match="socks5"):
        Settings(max_slots=3, egress_proxy="http://172.17.0.1:1080")
    with pytest.raises(ValueError, match="max_slots"):
        Settings(max_slots=5, egress_proxy="socks5://172.17.0.1:1080")
    ok = Settings(max_slots=3, egress_proxy="socks5://172.17.0.1:1080")
    assert [ok.display_for(i) for i in (1, 2, 3)] == [":99", ":100", ":101"]
    assert [ok.vnc_port_for(i) for i in (1, 2, 3)] == [5900, 5901, 5902]


def test_proxy_pac_only_for_slots_above_direct() -> None:
    s = Settings(max_slots=3, egress_proxy="socks5://172.17.0.1:1080")
    assert s.proxy_pac(1) is None
    assert s.proxy_pac(2) is None
    pac = s.proxy_pac(3)
    assert pac is not None and pac.startswith("data:application/x-ns-proxy-autoconfig;base64,")
    import base64

    script = base64.b64decode(pac.split(",", 1)[1]).decode()
    assert '"game.levelingidle2d.com.br"' in script
    assert 'return "SOCKS5 172.17.0.1:1080"' in script
    assert 'return "DIRECT"' in script  # o site e o resto saem pelo IP principal
    assert Settings().proxy_pac(3) is None  # sem proxy configurado, nada muda


def test_player_emails_join_the_allowlist() -> None:
    s = Settings(admin_emails=OWNER, player_emails=f" {PLAYER.upper()} ,")
    assert s.allowed_email_set == frozenset({OWNER, PLAYER})
    assert s.admin_email_set == frozenset({OWNER})


async def test_runtime_stop_closes_browser_and_keeps_switch(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    await store.set_power(True)
    driver = FakeDriver()

    async def no_creds() -> object:
        raise AssertionError("not used")

    record = SlotRecord(1, OWNER, "2026-11-03T00:00:00Z")
    sup = Supervisor(store, driver, no_creds)  # type: ignore[arg-type]
    task = asyncio.create_task(asyncio.sleep(3600))
    runtime = SlotRuntime(record, store, sup, driver, 5900, task=task)
    await runtime.stop()
    assert task.cancelled()
    assert driver.calls == ["close"]
    assert store.power.desired_on is True
