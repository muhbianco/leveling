"""Envio pela WuzAPI na borda (httpx.MockTransport): JID do /user/check primeiro, JID do número
por último com retentativas, mesmo Id em todas, nada sem configuração."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import whatsapp
from app.config import Settings
from app.whatsapp import SendResult, WuzapiMessenger, phone_variants

PHONE = "553491234567"


def make_settings(tmp_path: Path, configured: bool = True) -> Settings:
    secrets = tmp_path / "secrets.env"
    lines = "DATA_KEY=x\n"
    if configured:
        lines += "WUZAPI_BASE_URL=http://wuzapi:8080/\nWUZAPI_SESSION_TOKEN=session-token\n"
    secrets.write_text(lines, encoding="utf-8")
    return Settings(secrets_file=secrets, data_dir=tmp_path / "data")


class Wuzapi:
    def __init__(self, users: list[dict[str, Any]], send_status: dict[str, list[int]]) -> None:
        self.users = users
        self.send_status = send_status  # JID -> status de cada tentativa, em ordem
        self.sends: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["token"] == "session-token"
        body = json.loads(request.content)
        if request.url.path == "/user/check":
            self.checks.append(body)
            return httpx.Response(200, json={"data": {"Users": self.users}})
        if request.url.path == "/chat/send/text":
            self.sends.append(body)
            codes = self.send_status.get(body["Phone"], [200])
            return httpx.Response(codes.pop(0) if len(codes) > 1 else codes[0], json={})
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(whatsapp, "_sleep", instant)


def test_phone_variants() -> None:
    assert phone_variants("553491234567") == ["553491234567", "5534991234567"]
    assert phone_variants("5512988887777") == ["5512988887777", "551288887777"]
    assert phone_variants("551133334444") == ["551133334444"]  # fixo


async def test_sends_to_jid_from_check_first(tmp_path: Path) -> None:
    api = Wuzapi([{"Query": PHONE, "IsInWhatsapp": True, "JID": "123@lid"}], {})
    messenger = WuzapiMessenger(make_settings(tmp_path), httpx.MockTransport(api.handler))
    assert await messenger.send(PHONE, "oi") is SendResult.SENT
    assert api.checks == [{"Phone": ["553491234567", "5534991234567"]}]
    assert [s["Phone"] for s in api.sends] == ["123@lid"]
    assert api.sends[0]["Body"] == "oi"
    assert api.sends[0]["LinkPreview"] is False


async def test_falls_back_to_phone_jid_with_retries_same_id(tmp_path: Path) -> None:
    api = Wuzapi(
        [{"Query": PHONE, "IsInWhatsapp": True, "JID": "123@lid"}],
        {"123@lid": [400], f"{PHONE}@s.whatsapp.net": [502, 200]},
    )
    messenger = WuzapiMessenger(make_settings(tmp_path), httpx.MockTransport(api.handler))
    assert await messenger.send(PHONE, "oi") is SendResult.SENT
    assert [s["Phone"] for s in api.sends] == ["123@lid", *[f"{PHONE}@s.whatsapp.net"] * 2]
    assert len({s["Id"] for s in api.sends}) == 1  # repetir não duplica no WhatsApp


async def test_gives_up_after_bounded_attempts(tmp_path: Path) -> None:
    api = Wuzapi([], {f"{PHONE}@s.whatsapp.net": [500]})
    messenger = WuzapiMessenger(make_settings(tmp_path), httpx.MockTransport(api.handler))
    assert await messenger.send(PHONE, "oi") is SendResult.FAILED
    assert len(api.sends) == whatsapp.SEND_ATTEMPTS


async def test_network_error_is_a_failure_not_a_crash(tmp_path: Path) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    messenger = WuzapiMessenger(make_settings(tmp_path), httpx.MockTransport(down))
    assert await messenger.send(PHONE, "oi") is SendResult.FAILED


async def test_not_configured_sends_nothing(tmp_path: Path) -> None:
    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError("não devia chamar a WuzAPI")

    messenger = WuzapiMessenger(make_settings(tmp_path, False), httpx.MockTransport(never))
    assert await messenger.send(PHONE, "oi") is SendResult.NOT_CONFIGURED
    assert await messenger.send("", "oi") is SendResult.NO_PHONE
