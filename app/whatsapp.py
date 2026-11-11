"""WhatsApp pela WuzAPI, na sessão do MegaPromoOz (token da SESSÃO, nunca o de admin), a mesma que
o sesi-vagas usa. Só manda o pedido de acesso à tela remota ao dono da instância e o aviso de
"liberado" a quem pediu.

Contrato conferido no sesi-vagas contra o spec da imagem em uso (asternic/wuzapi:1.0.9,
static/api/spec.yml):
- `POST /user/check` {"Phone": [...]} devolve o JID de cada número. Pode vir sem o nono dígito ou
  como `@lid` (medido em 19/10/2026): o JID não é o telefone. O envio tenta o JID devolvido e cai
  para `<número>@s.whatsapp.net`.
- `POST /chat/send/text` {"Phone", "Body", "LinkPreview", "Id"}. O `Id` é nosso: repetir depois de
  um timeout reaproveita o Id e o WhatsApp não mostra a mensagem duas vezes.

URL e token vêm do arquivo de segredos, relido a cada envio. Nunca loga token nem número inteiro.
"""

from __future__ import annotations

import asyncio
import logging
import random
import secrets
from enum import StrEnum
from typing import Any, Protocol

import httpx

from app.config import Settings, read_secrets

logger = logging.getLogger(__name__)

SEND_ATTEMPTS = 3
# Espera entre tentativas (os testes trocam por uma que não dorme).
_sleep = asyncio.sleep


class SendResult(StrEnum):
    SENT = "enviado"
    FAILED = "falhou"
    NO_PHONE = "sem_telefone"
    NOT_CONFIGURED = "sem_whatsapp"


class Messenger(Protocol):
    async def send(self, phone: str, text: str) -> SendResult: ...


def masked(value: str) -> str:
    user, _, server = value.partition("@")
    out = f"{user[:4]}…{user[-4:]}" if len(user) > 8 else "…"
    return f"{out}@{server}" if server else out


def phone_variants(phone: str) -> list[str]:
    """Celular do Brasil com e sem o nono dígito: o WhatsApp de vários DDDs ainda guarda o número
    com 8 dígitos. Fixo (começa com 2 a 5) não ganha variante."""
    if len(phone) == 12 and phone[4] in "6789":
        return [phone, f"{phone[:4]}9{phone[4:]}"]
    if len(phone) == 13 and phone[4] == "9":
        return [phone, f"{phone[:4]}{phone[5:]}"]
    return [phone]


class WuzapiMessenger:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def send(self, phone: str, text: str) -> SendResult:
        if not phone:
            return SendResult.NO_PHONE
        cfg = await asyncio.to_thread(read_secrets, self._settings.secrets_file)
        if not cfg.wuzapi_base_url or not cfg.wuzapi_session_token:
            logger.warning("whatsapp not configured (WUZAPI_* missing from the secrets file)")
            return SendResult.NOT_CONFIGURED
        base = cfg.wuzapi_base_url.rstrip("/")
        headers = {"token": cfg.wuzapi_session_token, "Authorization": cfg.wuzapi_session_token}
        msg_id = secrets.token_hex(16).upper()
        async with httpx.AsyncClient(
            base_url=base,
            headers=headers,
            timeout=self._settings.wuzapi_timeout_seconds,
            transport=self._transport,
        ) as client:
            targets = await self._targets(client, phone)
            for index, jid in enumerate(targets):
                # Só o último (o JID do número) espera o backoff: os outros têm uma tentativa.
                attempts = SEND_ATTEMPTS if index == len(targets) - 1 else 1
                if await self._send_to(client, jid, text, msg_id, attempts):
                    return SendResult.SENT
        return SendResult.FAILED

    async def _targets(self, client: httpx.AsyncClient, phone: str) -> list[str]:
        """JIDs a tentar, em ordem: os que o /user/check devolveu, depois o JID do número
        configurado (sempre por último, com as retentativas)."""
        found: list[str] = []
        try:
            response = await client.post("/user/check", json={"Phone": phone_variants(phone)})
            users: list[dict[str, Any]] = (response.json().get("data") or {}).get("Users") or []
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            logger.warning("wuzapi /user/check failed: %s", type(exc).__name__)
            users = []
        else:
            if response.status_code >= 300:
                logger.warning("wuzapi /user/check answered %s", response.status_code)
                users = []
        for user in users:
            jid = str(user.get("JID") or "")
            if user.get("IsInWhatsapp") and jid and jid not in found:
                found.append(jid)
        if users and not found:
            logger.warning("whatsapp %s: not on WhatsApp per /user/check", masked(phone))
        fallback = f"{phone}@s.whatsapp.net"
        return [j for j in found if j != fallback] + [fallback]

    async def _send_to(
        self, client: httpx.AsyncClient, jid: str, text: str, msg_id: str, attempts: int
    ) -> bool:
        body = {"Phone": jid, "Body": text, "LinkPreview": False, "Id": msg_id}
        problem = ""
        for attempt in range(1, attempts + 1):
            try:
                response = await client.post("/chat/send/text", json=body)
            except httpx.HTTPError as exc:
                problem = type(exc).__name__
            else:
                if response.status_code < 300:
                    logger.info("whatsapp sent to %s attempt=%d", masked(jid), attempt)
                    return True
                problem = f"HTTP {response.status_code}"
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    break  # recusa do pedido: repetir não muda
            if attempt < attempts:
                await _sleep(2**attempt + random.uniform(0, 1))  # noqa: S311 (jitter)
        logger.warning("whatsapp to %s failed: %s", masked(jid), problem)
        return False
