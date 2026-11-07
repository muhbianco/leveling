from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from app.egress import (
    REPLY_ATYP_UNSUPPORTED,
    REPLY_COMMAND_UNSUPPORTED,
    REPLY_HOST_UNREACHABLE,
    REPLY_NOT_ALLOWED,
    REPLY_OK,
    REPLY_REFUSED,
    EgressConfig,
    EgressError,
    Socks5Server,
    parse_allow,
)


async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(1024):
        writer.write(data)
        await writer.drain()
    writer.close()


@pytest.fixture
async def upstream() -> AsyncIterator[int]:
    server = await asyncio.start_server(_echo, "127.0.0.1", 0)
    port: int = server.sockets[0].getsockname()[1]
    async with server:
        yield port


@pytest.fixture
async def proxy(upstream: int) -> AsyncIterator[tuple[Socks5Server, int]]:
    cfg = EgressConfig(
        listen_host="127.0.0.1",
        listen_port=0,
        bind="127.0.0.1",
        allow=frozenset({("localhost", upstream), ("127.0.0.1", upstream)}),
        connect_timeout=2.0,
        max_connections=4,
    )
    server = Socks5Server(cfg)
    _host, port = await server.start()
    yield server, port
    await server.close()


async def socks_connect(
    proxy_port: int, host: str, port: int, *, command: int = 1, atyp: int = 3
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, int]:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(b"\x05\x01\x00")
    await writer.drain()
    assert await reader.readexactly(2) == b"\x05\x00"
    if atyp == 3:
        addr = bytes([len(host)]) + host.encode()
    else:
        addr = bytes(int(p) for p in host.split("."))
    writer.write(bytes([5, command, 0, atyp]) + addr + port.to_bytes(2, "big"))
    await writer.drain()
    reply = await reader.readexactly(10)
    return reader, writer, reply[1]


async def test_connect_allowed_host_and_echo(
    proxy: tuple[Socks5Server, int], upstream: int
) -> None:
    _server, port = proxy
    reader, writer, code = await socks_connect(port, "localhost", upstream)
    assert code == REPLY_OK
    writer.write(b"ola jogo")
    await writer.drain()
    assert await reader.readexactly(8) == b"ola jogo"
    writer.close()
    await writer.wait_closed()


async def test_ipv4_literal_allowed(proxy: tuple[Socks5Server, int], upstream: int) -> None:
    _server, port = proxy
    reader, writer, code = await socks_connect(port, "127.0.0.1", upstream, atyp=1)
    assert code == REPLY_OK
    writer.write(b"x")
    await writer.drain()
    assert await reader.readexactly(1) == b"x"
    writer.close()


async def test_other_destinations_are_refused(
    proxy: tuple[Socks5Server, int], upstream: int
) -> None:
    _server, port = proxy
    for host, dport in [("example.com", 443), ("localhost", upstream + 1)]:
        reader, _writer, code = await socks_connect(port, host, dport)
        assert code == REPLY_NOT_ALLOWED
        assert await reader.read() == b""  # fecha depois da recusa


async def test_unsupported_command_and_atyp(proxy: tuple[Socks5Server, int], upstream: int) -> None:
    _server, port = proxy
    _r, _w, code = await socks_connect(port, "localhost", upstream, command=2)  # BIND
    assert code == REPLY_COMMAND_UNSUPPORTED
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"\x05\x01\x00")
    await writer.drain()
    await reader.readexactly(2)
    writer.write(b"\x05\x01\x00\x04" + bytes(16) + b"\x01\xbb")  # IPv6
    await writer.drain()
    assert (await reader.readexactly(10))[1] == REPLY_ATYP_UNSUPPORTED


async def test_auth_required_by_client_is_refused(proxy: tuple[Socks5Server, int]) -> None:
    _server, port = proxy
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"\x05\x01\x02")  # só usuário/senha
    await writer.drain()
    assert await reader.readexactly(2) == b"\x05\xff"


async def test_closed_upstream_is_reported(upstream: int) -> None:
    # Porta permitida, mas ninguém escutando: o cliente recebe recusa em vez de ficar pendurado.
    probe = await asyncio.start_server(_echo, "127.0.0.1", 0)
    dead_port: int = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()
    cfg = EgressConfig("127.0.0.1", 0, "127.0.0.1", frozenset({("localhost", dead_port)}), 5.0)
    server = Socks5Server(cfg)
    _host, port = await server.start()
    try:
        _r, _w, code = await socks_connect(port, "localhost", dead_port)
        # Linux recusa na hora; o Windows tenta de novo por ~2 s antes de recusar.
        assert code in (REPLY_REFUSED, REPLY_HOST_UNREACHABLE)
    finally:
        await server.close()


def test_config_from_env() -> None:
    cfg = EgressConfig.from_env(
        {
            "EGRESS_LISTEN": "172.17.0.1:1080",
            "EGRESS_BIND": "95.216.176.17",
            "EGRESS_ALLOW": "game.levelingidle2d.com.br:8443, Example.com:443",
        }
    )
    assert (cfg.listen_host, cfg.listen_port, cfg.bind) == ("172.17.0.1", 1080, "95.216.176.17")
    assert cfg.allow == {("game.levelingidle2d.com.br", 8443), ("example.com", 443)}
    assert cfg.max_connections == 64


@pytest.mark.parametrize(
    "env",
    [
        {"EGRESS_BIND": ""},
        {"EGRESS_BIND": "not-an-ip"},
        {"EGRESS_BIND": "95.216.176.17", "EGRESS_ALLOW": ""},
        {"EGRESS_BIND": "95.216.176.17", "EGRESS_ALLOW": "semporta"},
        {"EGRESS_BIND": "95.216.176.17", "EGRESS_LISTEN": "1080"},
    ],
)
def test_config_rejects_bad_values(env: dict[str, str]) -> None:
    with pytest.raises(EgressError):
        EgressConfig.from_env(env)


def test_parse_allow_lowercases_hosts() -> None:
    assert parse_allow("Game.Example:8443,,") == {("game.example", 8443)}
