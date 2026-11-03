"""Estado persistido: a chave liga/desliga, por que desligou e os últimos eventos.

Um processo só (uma réplica, um worker): o lock serializa as gravações. Cada gravação é atômica
(arquivo temporário + rename) para um kill no meio não deixar JSON cortado.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_EVENTS = 200


class OffReason(StrEnum):
    MANUAL = "manual"  # o dono desligou
    REPLACED = "assumido"  # a conta entrou em outro aparelho
    ERROR = "erro"  # erro que não se resolve insistindo


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class Event:
    at: str
    kind: str
    message: str


@dataclass(frozen=True, slots=True)
class PowerState:
    desired_on: bool
    off_reason: OffReason
    off_detail: str
    changed_at: str


class StateStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._power = PowerState(False, OffReason.MANUAL, "", utcnow_iso())
        self._events: deque[Event] = deque(maxlen=MAX_EVENTS)

    @property
    def power(self) -> PowerState:
        return self._power

    def events(self, limit: int) -> list[Event]:
        """Mais recentes primeiro."""
        return list(reversed(self._events))[: max(0, limit)]

    async def load(self) -> None:
        raw = await asyncio.to_thread(self._read)
        if raw is None:
            return
        try:
            self._power = PowerState(
                desired_on=bool(raw["desired_on"]),
                off_reason=OffReason(raw.get("off_reason", OffReason.MANUAL)),
                off_detail=str(raw.get("off_detail", "")),
                changed_at=str(raw.get("changed_at", utcnow_iso())),
            )
            self._events.extend(
                Event(at=str(e["at"]), kind=str(e["kind"]), message=str(e["message"]))
                for e in raw.get("events", [])[-MAX_EVENTS:]
            )
        except (KeyError, TypeError, ValueError) as exc:
            # Arquivo de outra versão ou corrompido: começa desligado em vez de adivinhar.
            logger.warning("state file unreadable, starting off: %s", type(exc).__name__)
            self._power = PowerState(False, OffReason.MANUAL, "", utcnow_iso())
            self._events.clear()

    async def set_power(
        self, on: bool, reason: OffReason = OffReason.MANUAL, detail: str = ""
    ) -> bool:
        """Devolve True se mudou alguma coisa."""
        async with self._lock:
            current = self._power
            if on and current.desired_on:
                return False
            if not on and not current.desired_on and current.off_reason == reason:
                return False
            self._power = PowerState(
                desired_on=on,
                off_reason=OffReason.MANUAL if on else reason,
                off_detail="" if on else detail[:300],
                changed_at=utcnow_iso(),
            )
            if on:
                message = "Ligado: o servidor assume a sessão"
                kind = "ligado"
            elif reason is OffReason.REPLACED:
                message = "Desligado sozinho: a conta entrou em outro aparelho"
                kind = "assumido"
            elif reason is OffReason.ERROR:
                message = f"Desligado por erro: {detail[:300]}" if detail else "Desligado por erro"
                kind = "erro"
            else:
                message = "Desligado"
                kind = "desligado"
            self._events.append(Event(utcnow_iso(), kind, message))
            await self._save()
            return True

    async def add_event(self, kind: str, message: str) -> None:
        async with self._lock:
            self._events.append(Event(utcnow_iso(), kind, message[:300]))
            await self._save()

    async def _save(self) -> None:
        payload: dict[str, Any] = {
            "desired_on": self._power.desired_on,
            "off_reason": self._power.off_reason.value,
            "off_detail": self._power.off_detail,
            "changed_at": self._power.changed_at,
            "events": [asdict(e) for e in self._events],
        }
        await asyncio.to_thread(self._write, payload)

    def _read(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.warning("state file unreadable, starting off: %s", type(exc).__name__)
            return None
        return data if isinstance(data, dict) else None

    def _write(self, payload: dict[str, Any]) -> None:
        write_json_atomic(self._path, payload)


def write_json_atomic(path: Path, payload: object) -> None:
    """Grava com permissão 600 (senha cifrada, hash de sessão): só o usuário do container lê."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
