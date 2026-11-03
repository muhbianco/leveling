from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.slots import (
    SOURCE_NONE,
    SOURCE_PANEL,
    SOURCE_SERVER,
    CredentialStore,
    SlotFullError,
    SlotManager,
    SlotRecord,
    SlotRegistry,
    SlotRuntime,
    credentials_provider,
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


def test_credentials_roundtrip_and_wrong_key(tmp_path: Path) -> None:
    registry = SlotRegistry(tmp_path / "data", max_slots=2)
    registry.load(None)
    record = registry.add(PLAYER)
    creds = CredentialStore(registry)
    creds.save(record.id, "pug", "pw com espaço ", KEY)

    stored = creds.read(record.id, KEY)
    assert stored is not None
    assert (stored.username, stored.password) == ("pug", "pw com espaço ")
    assert "pw com" not in repr(stored)
    assert creds.username(record.id) == "pug"
    assert creds.read(record.id, Fernet.generate_key().decode()) is None
    assert creds.read(record.id, "") is None
    if sys.platform != "win32":
        mode = stat.S_IMODE((registry.slot_dir(record.id) / "credentials.json").stat().st_mode)
        assert mode == 0o600


async def test_provider_prefers_panel_then_owner_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, f"GAME_USERNAME=owner\nGAME_PASSWORD=opw\nDATA_KEY={KEY}\n")
    registry = SlotRegistry(settings.data_dir, 2)
    registry.load(None)
    creds = CredentialStore(registry)
    owner = registry.add(OWNER)
    player = registry.add(PLAYER)

    owner_creds = await credentials_provider(settings, creds, owner)()
    assert (owner_creds.game_username, owner_creds.source) == ("owner", SOURCE_SERVER)
    player_creds = await credentials_provider(settings, creds, player)()
    assert (player_creds.has_game_credentials, player_creds.source) == (False, SOURCE_NONE)

    creds.save(owner.id, "owner-novo", "npw", KEY)
    updated = await credentials_provider(settings, creds, owner)()
    assert (updated.game_username, updated.game_password, updated.source) == (
        "owner-novo",
        "npw",
        SOURCE_PANEL,
    )


async def test_manager_skips_accounts_no_longer_allowed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, f"DATA_KEY={KEY}\n")
    registry = SlotRegistry(settings.data_dir, 2)
    registry.load(None)
    registry.add("removido@x.com")
    registry.add(PLAYER)
    creds = CredentialStore(registry)
    started: list[int] = []

    async def build(record: SlotRecord) -> SlotRuntime:
        started.append(record.id)
        store = StateStore(registry.slot_dir(record.id) / "state.json")
        driver = FakeDriver()
        sup = Supervisor(store, driver, credentials_provider(settings, creds, record))
        return SlotRuntime(record, store, sup, driver, 5900)

    manager = SlotManager(settings, SlotRegistry(settings.data_dir, 2), creds, build)
    await manager.start()
    assert started == [2]
    assert manager.for_email("removido@x.com") is None
    assert manager.capacity_left() == 0  # a vaga continua reservada


def test_slot_displays_and_ports_do_not_collide() -> None:
    s = Settings()
    assert [s.display_for(i) for i in (1, 2)] == [":99", ":100"]
    assert [s.vnc_port_for(i) for i in (1, 2)] == [5900, 5901]
    assert s.max_slots == 2


def test_max_slots_cannot_pass_the_game_limit() -> None:
    with pytest.raises(ValueError, match="max_slots"):
        Settings(max_slots=3)


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
