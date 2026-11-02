from __future__ import annotations

import json
from pathlib import Path

from app.state import MAX_EVENTS, OffReason, StateStore


async def test_power_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    await store.load()
    assert store.power.desired_on is False

    assert await store.set_power(True) is True
    assert await store.set_power(True) is False  # sem mudança, sem evento

    again = StateStore(path)
    await again.load()
    assert again.power.desired_on is True
    assert [e.kind for e in again.events(10)] == ["ligado"]


async def test_auto_off_keeps_reason(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    await store.set_power(True)
    await store.set_power(False, OffReason.REPLACED, "outro aparelho")
    assert store.power.off_reason is OffReason.REPLACED
    assert store.power.off_detail == "outro aparelho"
    assert store.events(1)[0].kind == "assumido"


async def test_events_are_capped_and_newest_first(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    for i in range(MAX_EVENTS + 20):
        await store.add_event("aviso", f"e{i}")
    events = store.events(1000)
    assert len(events) == MAX_EVENTS
    assert events[0].message == f"e{MAX_EVENTS + 19}"
    data = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert len(data["events"]) == MAX_EVENTS


async def test_corrupt_file_starts_off(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    store = StateStore(path)
    await store.load()
    assert store.power.desired_on is False

    path.write_text(json.dumps({"desired_on": True, "off_reason": "???"}), encoding="utf-8")
    store = StateStore(path)
    await store.load()
    assert store.power.desired_on is False


async def test_write_is_atomic_leaves_no_temp(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    await store.set_power(True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]
