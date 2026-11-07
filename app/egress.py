"""Proxy de saída SOCKS5 mínimo: o slot acima de DIRECT_SLOTS manda o tráfego do servidor do jogo
por aqui, e daqui ele sai pelo segundo IPv4 da hel1 (EGRESS_BIND, o floating IP). O jogo conta
2 contas por IP e derruba todas acima disso; com dois IPs, 3 pessoas jogam de um servidor só.

Roda na rede do host (serviço `egress` em infra/docker-stack.yml), escuta só no endereço do
docker0 (os containers chegam, a internet não) e só conecta nos destinos de EGRESS_ALLOW. Sem
autenticação: quem alcança 172.17.0.1 já está dentro da hel1. Só CONNECT, só IPv4.

    python -m app.egress
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

logger = logging.getLogger("egress")

SOCKS_VERSION = 5
NO_AUTH = 0x00
NO_ACCEPTABLE_METHODS = 0xFF
CMD_CONNECT = 0x01
ATYP_IPV4 = 0x01
ATYP_DOMAIN = 0x03
REPLY_OK = 0x00
REPLY_FAILURE = 0x01
REPLY_NOT_ALLOWED = 0x02
REPLY_HOST_UNREACHABLE = 0x04
REPLY_REFUSED = 0x05
REPLY_COMMAND_UNSUPPORTED = 0x07
REPLY_ATYP_UNSUPPORTED = 0x08

HANDSHAKE_TIMEOUT_S = 10.0
COPY_CHUNK = 64 * 1024


class EgressError(Exception):
    """Configuração inválida: o serviço não sobe."""


@dataclass(frozen=True)
class EgressConfig:
    listen_host: str
    listen_port: int
    bind: str  # IPv4 de origem das conexões de saída (o floating IP)
    allow: frozenset[tuple[str, int]] = field(default_factory=frozenset)  # (host, porta)
    connect_timeout: float = 10.0
    max_connections: int = 64

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> EgressConfig:
        if env is None:
            env = os.environ
        listen = env.get("EGRESS_LISTEN", "172.17.0.1:1080")
        host, _, port = listen.rpartition(":")
        if not host or not port.isdigit():
            raise EgressError("EGRESS_LISTEN precisa ser host:porta")
        bind = env.get("EGRESS_BIND", "").strip()
        if not bind:
            raise EgressError("EGRESS_BIND (IPv4 de saída) é obrigatório")
        try:
            socket.inet_aton(bind)
        except OSError as exc:
            raise EgressError("EGRESS_BIND precisa ser um IPv4") from exc
        allow = parse_allow(env.get("EGRESS_ALLOW", "game.levelingidle2d.com.br:8443"))
        if not allow:
            raise EgressError("EGRESS_ALLOW vazio: nenhum destino permitido")
        return cls(
            listen_host=host,
            listen_port=int(port),
            bind=bind,
            allow=allow,
            connect_timeout=float(env.get("EGRESS_CONNECT_TIMEOUT", "10")),
            max_connections=int(env.get("EGRESS_MAX_CONNECTIONS", "64")),
        )


def parse_allow(raw: str) -> frozenset[tuple[str, int]]:
    allow: set[tuple[str, int]] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        host, sep, port = item.rpartition(":")
        if not sep or not host or not port.isdigit():
            raise EgressError(f"destino inválido em EGRESS_ALLOW: {item!r} (use host:porta)")
        allow.add((host.lower(), int(port)))
    return frozenset(allow)


class Socks5Server:
    def __init__(self, cfg: EgressConfig) -> None:
        self._cfg = cfg
        self._slots = asyncio.Semaphore(cfg.max_connections)
        self._server: asyncio.base_events.Server | None = None
        self.active = 0

    async def start(self) -> tuple[str, int]:
        self._server = await asyncio.start_server(
            self._handle, self._cfg.listen_host, self._cfg.listen_port
        )
        host, port = self._server.sockets[0].getsockname()[:2]
        logger.info(
            "listening on %s:%s, egress via %s, %d destination(s) allowed",
            host,
            port,
            self._cfg.bind,
            len(self._cfg.allow),
        )
        return str(host), int(port)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # --- uma conexão ------------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._slots.locked():
            logger.warning("too many connections; refusing")
            writer.close()
            return
        async with self._slots:
            self.active += 1
            try:
                await self._session(reader, writer)
            except (OSError, asyncio.IncompleteReadError, TimeoutError) as exc:
                logger.info("session ended: %s", type(exc).__name__)
            finally:
                self.active -= 1
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async with asyncio.timeout(HANDSHAKE_TIMEOUT_S):
            target = await self._handshake(reader, writer)
        if target is None:
            return
        host, port = target
        started = time.monotonic()
        try:
            upstream_r, upstream_w = await asyncio.wait_for(
                asyncio.open_connection(
                    host, port, family=socket.AF_INET, local_addr=(self._cfg.bind, 0)
                ),
                self._cfg.connect_timeout,
            )
        except TimeoutError:
            await _reply(writer, REPLY_HOST_UNREACHABLE)
            logger.warning("connect %s:%s timed out", host, port)
            return
        except OSError as exc:
            await _reply(writer, REPLY_REFUSED)
            logger.warning("connect %s:%s failed: %s", host, port, type(exc).__name__)
            return
        await _reply(writer, REPLY_OK)
        logger.info("open %s:%s", host, port)
        try:
            await _pipe(reader, writer, upstream_r, upstream_w)
        finally:
            upstream_w.close()
            with contextlib.suppress(OSError):
                await upstream_w.wait_closed()
            logger.info("close %s:%s after %.0fs", host, port, time.monotonic() - started)

    async def _handshake(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> tuple[str, int] | None:
        version, nmethods = await reader.readexactly(2)
        methods = await reader.readexactly(nmethods)
        if version != SOCKS_VERSION or NO_AUTH not in methods:
            writer.write(bytes([SOCKS_VERSION, NO_ACCEPTABLE_METHODS]))
            await writer.drain()
            return None
        writer.write(bytes([SOCKS_VERSION, NO_AUTH]))
        await writer.drain()

        version, command, _reserved, atyp = await reader.readexactly(4)
        if version != SOCKS_VERSION:
            return None
        if atyp == ATYP_IPV4:
            host = socket.inet_ntoa(await reader.readexactly(4))
        elif atyp == ATYP_DOMAIN:
            (length,) = await reader.readexactly(1)
            # Nome como o cliente mandou (ASCII; nada fora disso bate na lista de permitidos).
            host = (await reader.readexactly(length)).decode("ascii", errors="replace").lower()
        else:
            await _reply(writer, REPLY_ATYP_UNSUPPORTED)
            return None
        port = int.from_bytes(await reader.readexactly(2), "big")
        if command != CMD_CONNECT:
            await _reply(writer, REPLY_COMMAND_UNSUPPORTED)
            return None
        if (host, port) not in self._cfg.allow:
            await _reply(writer, REPLY_NOT_ALLOWED)
            logger.warning("refused destination %s:%s", host, port)
            return None
        return host, port


async def _reply(writer: asyncio.StreamWriter, code: int) -> None:
    # Endereço de retorno 0.0.0.0:0: o cliente (Chromium) não usa.
    writer.write(bytes([SOCKS_VERSION, code, 0x00, ATYP_IPV4, 0, 0, 0, 0, 0, 0]))
    await writer.drain()


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(COPY_CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    finally:
        if writer.can_write_eof():
            with contextlib.suppress(OSError, RuntimeError):
                writer.write_eof()


async def _pipe(
    client_r: asyncio.StreamReader,
    client_w: asyncio.StreamWriter,
    upstream_r: asyncio.StreamReader,
    upstream_w: asyncio.StreamWriter,
) -> None:
    tasks = [
        asyncio.create_task(_copy(client_r, upstream_w)),
        asyncio.create_task(_copy(upstream_r, client_w)),
    ]
    try:
        # Quando um lado fecha, o outro ainda pode ter dados a entregar: espera os dois.
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for task in tasks:
            task.cancel()


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = EgressConfig.from_env()
    await Socks5Server(cfg).serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
