"""Acesso à tela remota da instância de outro jogador, nos dois modos: pedir acesso (convite no
WhatsApp, aceite pelo dono logado, tempo contado do aceite) e abrir a própria instância (para um
jogador ou para todos, com aviso a quem recebe). Prazo, fechar e a ponte VNC respeitando tudo."""

from __future__ import annotations

import json
import re
import socketserver
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.access import INVITE_TTL_S, AccessService
from app.auth import SESSION_COOKIE
from app.config import Settings
from app.whatsapp import SendResult
from tests.test_api import (
    CLIENT_ID,
    ORIGIN,
    FakeGoogle,
    World,
    build_world,
    create_instance,
    login,
    write,
    write_secrets,
)

OWNER = "mear.mind@gmail.com"
ACAIS = "pugtimothy@gmail.com"
ERUDHIR = "erudhir@example.com"
PLAYER = "player@example.com"  # liberado, mas não admin
ACAIS_PHONE = "5512988887777"
ERUDHIR_PHONE = "553491234567"
LINK = re.compile(r"https://leveling\.test/acesso/([A-Za-z0-9_-]{20,64})")


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    secrets = tmp_path / "secrets.env"
    write_secrets(secrets)
    return Settings(
        public_url=ORIGIN,
        admin_emails=f"{OWNER},{ACAIS},{ERUDHIR}",
        player_emails=PLAYER,
        player_phones=f"{ACAIS}=+55 12 98888-7777, {ERUDHIR}=+55 34 9123-4567",
        max_slots=4,
        direct_slots=4,
        google_client_id=CLIENT_ID,
        secrets_file=secrets,
        data_dir=tmp_path / "data",
        manage_display=False,
        novnc_dir=tmp_path,
    )


@pytest.fixture
def world(settings: Settings, google: FakeGoogle) -> World:
    return build_world(settings, google)


def browser(world: World) -> Iterator[TestClient]:
    with TestClient(world.app, base_url=ORIGIN, follow_redirects=False) as c:
        yield c


@pytest.fixture
def owner(world: World) -> Iterator[TestClient]:
    """O dono da instância que recebe o pedido (Acais)."""
    yield from browser(world)


@pytest.fixture
def asker(world: World) -> Iterator[TestClient]:
    """Quem pede acesso (Erudhir)."""
    yield from browser(world)


@pytest.fixture
def third(world: World) -> Iterator[TestClient]:
    yield from browser(world)


def status(client: TestClient) -> dict[str, Any]:
    res = client.get("/api/status")
    assert res.status_code == 200
    body: dict[str, Any] = res.json()
    return body


def shared_entry(client: TestClient, slot_id: int) -> dict[str, Any]:
    shared = status(client)["access"]["shared"]
    entry: dict[str, Any] = next(e for e in shared if e["slot_id"] == slot_id)
    return entry


def wait_notified(client: TestClient, slot_id: int) -> dict[str, Any]:
    """O aviso sai em segundo plano: espera o estado sair de "enviando"."""
    for _ in range(100):
        access: dict[str, Any] = shared_entry(client, slot_id)["access"]
        if access.get("notify") != "enviando":
            return access
        threading.Event().wait(0.02)
    raise AssertionError("aviso não terminou")


def ask(
    client: TestClient,
    csrf: str,
    slot_id: int,
    duration: str = "1h",
    reason: str = "ver a caçada",
) -> Any:
    body = {"slot": slot_id, "duration": duration, "reason": reason}
    return client.post("/api/access/requests", json=body, headers=write(csrf))


def open_to(
    client: TestClient, csrf: str, audience: str, duration: str = "1h", reason: str = "olhar"
) -> Any:
    body = {"audience": audience, "duration": duration, "reason": reason}
    return client.post("/api/access/grants", json=body, headers=write(csrf))


def wait_sent(world: World, count: int) -> list[tuple[str, str]]:
    """Os avisos de acesso saem em segundo plano: espera chegarem `count` mensagens."""
    for _ in range(100):
        if len(world.messenger.sent) >= count:
            return world.messenger.sent
        threading.Event().wait(0.02)
    raise AssertionError(f"esperava {count} mensagens, vieram {len(world.messenger.sent)}")


class Scene:
    """Acais com instância (slot 1) e Erudhir logado, pedindo acesso a ela."""

    def __init__(
        self, world: World, google: FakeGoogle, owner: TestClient, asker: TestClient
    ) -> None:
        self.world = world
        self.owner_csrf = login(owner, google, ACAIS)
        self.slot_id = create_instance(owner, self.owner_csrf, "Acais")["slot"]["id"]
        self.asker_csrf = login(asker, google, ERUDHIR)
        self.owner = owner
        self.asker = asker

    def request(self, duration: str = "1h", reason: str = "ver a caçada") -> tuple[str, str]:
        """Pede, espera o WhatsApp e devolve (id do pedido, token do link)."""
        res = ask(self.asker, self.asker_csrf, self.slot_id, duration, reason)
        assert res.status_code == 200, res.text
        access = wait_notified(self.asker, self.slot_id)
        assert access["state"] == "pendente"
        phone, text = self.world.messenger.sent[-1]
        assert phone == ACAIS_PHONE
        found = LINK.search(text)
        assert found is not None
        return access["request_id"], found.group(1)

    def approve(self, request_id: str) -> Any:
        return self.owner.post(
            f"/api/access/requests/{request_id}/approve", headers=write(self.owner_csrf)
        )


@pytest.fixture
def scene(world: World, google: FakeGoogle, owner: TestClient, asker: TestClient) -> Scene:
    return Scene(world, google, owner, asker)


# --- pedido ------------------------------------------------------------------------------------


def test_request_sends_link_to_owner_whatsapp(scene: Scene, world: World) -> None:
    _request_id, token = scene.request("4h", "  montar o\n equipamento  ")
    phone, text = world.messenger.sent[0]
    assert phone == ACAIS_PHONE
    assert ERUDHIR in text and "tela remota" in text
    assert "por 4 horas" in text and "Motivo: montar o equipamento" in text
    assert shared_entry(scene.asker, scene.slot_id)["access"]["notify"] == "enviado"
    # No disco fica só o hash do token.
    stored = (world.slots._settings.data_dir / "access.json").read_text(encoding="utf-8")
    assert token not in stored
    # O pedido aparece no painel do dono também.
    incoming = status(scene.owner)["access"]["incoming"]
    assert [(r["email"], r["duration"], r["reason"]) for r in incoming] == [
        (ERUDHIR, "4h", "montar o equipamento")
    ]
    events = scene.owner.get("/api/events").json()["events"]
    assert any("pediu acesso" in e["message"] for e in events)


def test_shared_panel_is_admin_only(
    world: World, google: FakeGoogle, scene: Scene, third: TestClient
) -> None:
    assert status(scene.asker)["access"]["admin"] is True
    assert [e["slot_id"] for e in status(scene.asker)["access"]["shared"]] == [scene.slot_id]
    # A própria instância não aparece na lista dos outros.
    assert status(scene.owner)["access"]["shared"] == []
    csrf = login(third, google, PLAYER)
    body = status(third)
    assert body["access"]["admin"] is False
    assert body["access"]["shared"] is None
    res = ask(third, csrf, scene.slot_id)
    assert (res.status_code, res.json()["detail"]) == (403, "nao_admin")
    assert world.messenger.sent == []


def test_request_refusals(scene: Scene) -> None:
    own = ask(scene.owner, scene.owner_csrf, scene.slot_id)
    assert (own.status_code, own.json()["detail"]) == (409, "proprio_slot")
    missing = ask(scene.asker, scene.asker_csrf, 3)
    assert (missing.status_code, missing.json()["detail"]) == (404, "slot_nao_encontrado")
    scene.request()
    again = ask(scene.asker, scene.asker_csrf, scene.slot_id)
    assert (again.status_code, again.json()["detail"]) == (409, "pedido_pendente")
    assert ask(scene.asker, "x", scene.slot_id).status_code == 403  # CSRF


def test_failed_whatsapp_lets_ask_again(scene: Scene, world: World) -> None:
    world.messenger.result = SendResult.FAILED
    assert ask(scene.asker, scene.asker_csrf, scene.slot_id).status_code == 200
    assert wait_notified(scene.asker, scene.slot_id)["notify"] == "falhou"
    world.messenger.result = SendResult.SENT
    request_id, _token = scene.request()
    assert len(world.messenger.sent) == 2
    assert [r["id"] for r in status(scene.owner)["access"]["incoming"]] == [request_id]


def test_owner_without_phone_still_sees_the_request(
    world: World, google: FakeGoogle, owner: TestClient, asker: TestClient
) -> None:
    csrf = login(owner, google, OWNER)
    slot_id = create_instance(owner, csrf, "Mear")["slot"]["id"]
    asker_csrf = login(asker, google, ERUDHIR)
    assert ask(asker, asker_csrf, slot_id).status_code == 200
    assert wait_notified(asker, slot_id)["notify"] == "sem_telefone"
    assert world.messenger.sent == []
    assert len(status(owner)["access"]["incoming"]) == 1


def test_requester_cancels(scene: Scene) -> None:
    request_id, _token = scene.request()
    res = scene.asker.delete(f"/api/access/requests/{request_id}", headers=write(scene.asker_csrf))
    assert res.status_code == 200
    assert shared_entry(scene.asker, scene.slot_id)["access"]["state"] == "nenhum"
    assert status(scene.owner)["access"]["incoming"] == []
    # O dono não "cancela": ele recusa.
    request_id, _token = scene.request()
    res = scene.owner.delete(f"/api/access/requests/{request_id}", headers=write(scene.owner_csrf))
    assert res.status_code == 404


def test_request_expires(scene: Scene, world: World) -> None:
    scene.request()
    world.clock.now += INVITE_TTL_S + 1
    assert shared_entry(scene.asker, scene.slot_id)["access"]["state"] == "nenhum"
    assert status(scene.owner)["access"]["incoming"] == []


# --- link do WhatsApp ----------------------------------------------------------------------------


def test_link_without_session_goes_through_google_and_back(
    scene: Scene, world: World, google: FakeGoogle
) -> None:
    _request_id, token = scene.request()
    with TestClient(world.app, base_url=ORIGIN, follow_redirects=False) as phone:
        res = phone.get(f"/acesso/{token}")
        assert res.status_code == 303
        assert res.headers["location"] == f"/auth/login?next=%2Facesso%2F{token}"
        res = phone.get(res.headers["location"])
        query = parse_qs(urlsplit(res.headers["location"]).query)
        google.claims = {"nonce": query["nonce"][0], "email": ACAIS}
        back = phone.get("/auth/callback", params={"state": query["state"][0], "code": "abc"})
        assert (back.status_code, back.headers["location"]) == (303, f"/acesso/{token}")
        page = phone.get(f"/acesso/{token}")
        assert page.status_code == 200
        assert "/static/acesso.js" in page.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]


@pytest.mark.parametrize(
    "target", ["https://evil.test/x", "//evil.test", "/api/status", "/acesso/../api", "/"]
)
def test_login_return_is_allowlisted(
    world: World, google: FakeGoogle, owner: TestClient, target: str
) -> None:
    res = owner.get("/auth/login", params={"next": target})
    query = parse_qs(urlsplit(res.headers["location"]).query)
    google.claims = {"nonce": query["nonce"][0], "email": ACAIS}
    back = owner.get("/auth/callback", params={"state": query["state"][0], "code": "abc"})
    assert back.headers["location"] == "/"


def test_link_only_for_the_owner(scene: Scene, world: World, google: FakeGoogle) -> None:
    request_id, token = scene.request()
    mine = scene.owner.get(f"/api/access/link/{token}")
    assert mine.status_code == 200
    body = mine.json()
    assert body["request"]["id"] == request_id
    assert ERUDHIR in body["request"]["requester"]
    assert (body["request"]["duration"], body["request"]["reason"]) == ("1h", "ver a caçada")
    assert body["csrf"]
    # Link encaminhado: quem pediu (ou qualquer outro) não consegue liberar.
    other = scene.asker.get(f"/api/access/link/{token}")
    assert (other.status_code, other.json()["detail"]) == (403, "outra_conta")
    stolen = scene.asker.post(
        f"/api/access/requests/{request_id}/approve", headers=write(scene.asker_csrf)
    )
    assert (stolen.status_code, stolen.json()["detail"]) == (403, "outra_conta")
    assert scene.owner.get("/api/access/link/" + "x" * 32).status_code == 404
    assert scene.owner.get("/api/access/link/short").status_code == 422


# --- liberação -----------------------------------------------------------------------------------


def test_owner_approves_and_requester_opens_remote(
    scene: Scene, world: World, google: FakeGoogle, third: TestClient
) -> None:
    request_id, _token = scene.request("12h")
    assert scene.approve(request_id).status_code == 200
    granted = status(scene.owner)["access"]["granted"]
    assert [(g["audience"], g["origin"], g["duration"]) for g in granted] == [
        (ERUDHIR, "pedido", "12h")
    ]
    access = shared_entry(scene.asker, scene.slot_id)["access"]
    assert access["state"] == "liberado"
    assert access["remote_url"] == f"/remota?slot={scene.slot_id}"
    # Quem pediu recebe o aviso com o link da tela.
    phone, text = world.messenger.sent[-1]
    assert phone == ERUDHIR_PHONE
    assert f"/remota?slot={scene.slot_id}" in text

    page = scene.asker.get(f"/remota?slot={scene.slot_id}")
    assert page.status_code == 200
    info = scene.asker.get("/api/remote", params={"slot": scene.slot_id}).json()
    assert (info["own"], info["email"], info["slot_id"]) == (False, ACAIS, scene.slot_id)
    assert info["expires_at"].endswith("Z")
    # A própria tela continua sem `slot`.
    assert scene.owner.get("/api/remote").json()["own"] is True

    # Outro admin, sem acesso, não abre.
    login(third, google, OWNER)
    assert third.get(f"/remota?slot={scene.slot_id}").headers["location"] == "/"
    assert third.get("/api/remote", params={"slot": scene.slot_id}).status_code == 403


@pytest.mark.parametrize(
    ("duration", "reason"),
    [("5h", "ver a caçada"), ("1h", ""), ("1h", "ab"), ("1h", "x" * 201), ("1h", "a\x00bc")],
)
def test_request_validates_duration_and_reason(scene: Scene, duration: str, reason: str) -> None:
    res = ask(scene.asker, scene.asker_csrf, scene.slot_id, duration, reason)
    assert res.status_code == 422
    assert scene.world.messenger.sent == []


def test_approve_once(scene: Scene) -> None:
    request_id, _token = scene.request()
    assert scene.approve(request_id).status_code == 200
    assert scene.approve(request_id).status_code == 404  # já respondido


def test_access_time_starts_at_accept(scene: Scene, world: World) -> None:
    assert INVITE_TTL_S == 4 * 3600
    request_id, _token = scene.request("4h")
    world.clock.now += 3 * 3600  # convite ainda vale (4 h)
    assert scene.approve(request_id).status_code == 200
    world.clock.now += 3 * 3600  # 6 h depois do pedido, 3 h depois do aceite
    assert shared_entry(scene.asker, scene.slot_id)["access"]["state"] == "liberado"
    world.clock.now += 3600 + 1  # 4 h depois do aceite
    assert shared_entry(scene.asker, scene.slot_id)["access"]["state"] == "nenhum"


def test_grant_expires(scene: Scene, world: World) -> None:
    request_id, _token = scene.request()
    scene.approve(request_id)
    world.clock.now += 3600 + 1
    assert shared_entry(scene.asker, scene.slot_id)["access"]["state"] == "nenhum"
    assert scene.asker.get("/api/remote", params={"slot": scene.slot_id}).status_code == 403
    assert status(scene.owner)["access"]["granted"] == []


def test_deny_blocks_new_request_until_expiry(scene: Scene, world: World) -> None:
    request_id, _token = scene.request()
    res = scene.owner.post(
        f"/api/access/requests/{request_id}/deny", headers=write(scene.owner_csrf)
    )
    assert res.status_code == 200
    assert shared_entry(scene.asker, scene.slot_id)["access"]["state"] == "recusado"
    again = ask(scene.asker, scene.asker_csrf, scene.slot_id)
    assert (again.status_code, again.json()["detail"]) == (409, "pedido_recusado")
    world.clock.now += INVITE_TTL_S + 1
    scene.request()


def test_owner_revokes(scene: Scene) -> None:
    request_id, _token = scene.request()
    scene.approve(request_id)
    grant_id = status(scene.owner)["access"]["granted"][0]["id"]
    res = scene.owner.delete(f"/api/access/grants/{grant_id}", headers=write(scene.owner_csrf))
    assert res.status_code == 200
    assert scene.asker.get("/api/remote", params={"slot": scene.slot_id}).status_code == 403
    events = scene.owner.get("/api/events").json()["events"]
    assert any("encerrado pelo dono" in e["message"] for e in events)


def test_deleting_the_instance_ends_access(scene: Scene) -> None:
    request_id, _token = scene.request()
    scene.approve(request_id)
    assert scene.owner.delete("/api/slot", headers=write(scene.owner_csrf)).status_code == 200
    # Vaga recriada pelo mesmo dono: o acesso antigo não volta junto.
    create_instance(scene.owner, scene.owner_csrf, "Acais")
    assert shared_entry(scene.asker, scene.slot_id)["access"]["state"] == "nenhum"
    assert scene.asker.get("/api/remote", params={"slot": scene.slot_id}).status_code == 403


async def test_restart_reads_the_first_version_file(settings: Settings) -> None:
    """Formato do primeiro deploy: pedido sem tempo/motivo e acesso com `requester`. Um registro
    estragado sai sozinho; o envio interrompido vira "falhou"."""
    path = settings.data_dir / "access.json"
    path.parent.mkdir(parents=True)
    request = {
        "id": "a" * 12,
        "token_hash": "0" * 64,
        "requester": ERUDHIR,
        "owner": ACAIS,
        "slot_id": 1,
        "created_at": 1e12,
        "expires_at": 1e12 + 3600,
        "status": "pendente",
        "notify": "enviando",
    }
    grant = {
        "id": "b" * 12,
        "requester": ERUDHIR,
        "owner": ACAIS,
        "slot_id": 1,
        "granted_at": 1e12,
        "expires_at": 1e12 + 3600,
    }
    broken = {"id": "c" * 12}
    payload = {"requests": [request, broken], "grants": [grant, broken]}
    path.write_text(json.dumps(payload), encoding="utf-8")

    class NoSlots:
        def for_id(self, slot_id: int) -> None:
            return None

    service = AccessService(settings, NoSlots(), None, clock=lambda: 1e12)  # type: ignore[arg-type]
    await service.load()
    [req] = service._requests
    assert (req.notify, req.duration, req.reason) == ("falhou", "1h", "")
    [old] = service._grants
    assert (old.audience, old.origin, old.duration) == (ERUDHIR, "pedido", "1h")


# --- abrir minha instância -----------------------------------------------------------------------


def test_open_to_everyone_forever(
    scene: Scene, world: World, google: FakeGoogle, third: TestClient
) -> None:
    res = open_to(scene.owner, scene.owner_csrf, "*", "sempre", "deixar a caçada à vista")
    assert res.status_code == 200, res.text
    [grant] = res.json()["access"]["granted"]
    assert (grant["audience"], grant["origin"], grant["expires_at"]) == ("todos", "aberto", None)
    # Quem recebe é avisado: o Erudhir tem WhatsApp; o dono do servidor não tem número aqui.
    [(phone, text)] = wait_sent(world, 1)
    assert phone == ERUDHIR_PHONE
    assert "para todos os jogadores" in text and "sem prazo" in text
    assert "deixar a caçada à vista" in text and f"/remota?slot={scene.slot_id}" in text

    world.clock.now += 1000 * 86_400  # "sempre aberto" não vence
    access = shared_entry(scene.asker, scene.slot_id)["access"]
    assert (access["state"], access["scope"], access["expires_at"]) == ("liberado", "todos", None)
    info = scene.asker.get("/api/remote", params={"slot": scene.slot_id}).json()
    assert (info["scope"], info["expires_at"]) == ("todos", None)
    # Vale para qualquer admin, inclusive quem não estava na conversa.
    login(third, google, OWNER)
    assert third.get("/api/remote", params={"slot": scene.slot_id}).status_code == 200
    # Quem já tem acesso não precisa pedir.
    again = ask(scene.asker, scene.asker_csrf, scene.slot_id)
    assert (again.status_code, again.json()["detail"]) == (409, "ja_liberado")
    # Quem recebeu não fecha o "para todos"; o dono fecha.
    grant_id = access["grant_id"]
    url = f"/api/access/grants/{grant_id}"
    assert scene.asker.delete(url, headers=write(scene.asker_csrf)).status_code == 404
    assert scene.owner.delete(url, headers=write(scene.owner_csrf)).status_code == 200
    assert third.get("/api/remote", params={"slot": scene.slot_id}).status_code == 403
    messages = [e["message"] for e in scene.owner.get("/api/events").json()["events"]]
    assert any("aberta para todos os jogadores sem prazo" in m for m in messages)
    assert any("Acesso de todos os jogadores encerrado pelo dono" in m for m in messages)


def test_non_admin_never_gets_in_through_everyone(
    scene: Scene, google: FakeGoogle, third: TestClient
) -> None:
    open_to(scene.owner, scene.owner_csrf, "*", "sempre")
    login(third, google, PLAYER)
    assert third.get("/api/remote", params={"slot": scene.slot_id}).status_code == 403


def test_open_to_one_player(
    scene: Scene, world: World, google: FakeGoogle, third: TestClient
) -> None:
    scene.request()  # pedido pendente do Erudhir: abrir para ele resolve o pedido
    sent_before = len(world.messenger.sent)
    res = open_to(scene.owner, scene.owner_csrf, ERUDHIR.upper(), "12h", "ajuda no boss")
    assert res.status_code == 200
    body = res.json()["access"]
    assert body["incoming"] == []
    [grant] = body["granted"]
    assert (grant["audience"], grant["duration"]) == (ERUDHIR, "12h")
    phone, text = wait_sent(world, sent_before + 1)[-1]
    assert phone == ERUDHIR_PHONE and "para você" in text and "ajuda no boss" in text
    assert shared_entry(scene.asker, scene.slot_id)["access"]["scope"] == "voce"
    login(third, google, OWNER)
    assert third.get("/api/remote", params={"slot": scene.slot_id}).status_code == 403
    # Abrir de novo para o mesmo destino troca tempo e motivo (continua um acesso só).
    again = open_to(scene.owner, scene.owner_csrf, ERUDHIR, "1h", "só uma olhada")
    [grant] = again.json()["access"]["granted"]
    assert (grant["duration"], grant["reason"]) == ("1h", "só uma olhada")
    world.clock.now += 3600 + 1
    assert scene.asker.get("/api/remote", params={"slot": scene.slot_id}).status_code == 403


def test_open_refusals(scene: Scene, world: World, google: FakeGoogle, third: TestClient) -> None:
    for audience, code in (
        (ACAIS, "destino_invalido"),
        (PLAYER, "destino_invalido"),
        ("ninguem@x.com", "destino_invalido"),
    ):
        res = open_to(scene.owner, scene.owner_csrf, audience)
        assert (res.status_code, res.json()["detail"]) == (422, code)
    assert open_to(scene.owner, scene.owner_csrf, "*", "2h").status_code == 422
    assert open_to(scene.owner, scene.owner_csrf, "*", "1h", "").status_code == 422
    assert open_to(scene.owner, "x", "*").status_code == 403  # CSRF
    # Sem instância não há o que abrir.
    res = open_to(scene.asker, scene.asker_csrf, "*")
    assert (res.status_code, res.json()["detail"]) == (409, "sem_instancia")
    assert world.messenger.sent == []


def test_owner_panel_lists_people_to_open_for(scene: Scene) -> None:
    people = status(scene.owner)["access"]["people"]
    assert [p["email"] for p in people] == [OWNER, ERUDHIR]
    assert status(scene.asker)["access"]["people"] == []  # sem instância


# --- ponte VNC -----------------------------------------------------------------------------------


class _FakeVnc(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.sendall(b"RFB 003.008\n")
        while self.request.recv(4096):
            pass


@pytest.fixture
def vnc_port() -> Iterator[int]:
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _FakeVnc)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_vnc_bridge_follows_the_grant(scene: Scene, world: World, vnc_port: int) -> None:
    runtime = world.slots.for_id(scene.slot_id)
    assert runtime is not None
    runtime.vnc_port = vnc_port
    url = f"/vnc/ws?slot={scene.slot_id}"
    # O websocket do TestClient vai para ws://testserver: o cookie `secure` do painel não vai
    # sozinho.
    cookie = scene.asker.cookies.get(SESSION_COOKIE)
    headers = {"Origin": ORIGIN, "Cookie": f"{SESSION_COOKIE}={cookie}"}

    # Sem acesso: recusado antes de abrir o TCP.
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        scene.asker.websocket_connect(url, headers=headers),
    ):
        pass
    assert refused.value.code == 1008

    request_id, _token = scene.request()
    scene.approve(request_id)
    grant_id = shared_entry(scene.asker, scene.slot_id)["access"]["grant_id"]
    with scene.asker.websocket_connect(url, headers=headers) as ws:
        assert ws.receive_bytes() == b"RFB 003.008\n"
        assert runtime.supervisor.viewers == 1
        # Quem recebeu abre mão: a tela aberta cai com 1008 (a página não insiste).
        res = scene.asker.delete(f"/api/access/grants/{grant_id}", headers=write(scene.asker_csrf))
        assert res.status_code == 200
        with pytest.raises(WebSocketDisconnect) as ended:
            ws.receive_bytes()
        assert ended.value.code == 1008
    for _ in range(100):
        if runtime.supervisor.viewers == 0:
            break
        threading.Event().wait(0.02)
    assert runtime.supervisor.viewers == 0
    messages = [e["message"] for e in scene.owner.get("/api/events").json()["events"]]
    assert any("abriu a tela remota" in m for m in messages)
    assert any("fechou a tela remota" in m for m in messages)


def test_vnc_bridge_slot_param(scene: Scene, world: World, vnc_port: int) -> None:
    runtime = world.slots.for_id(scene.slot_id)
    assert runtime is not None
    runtime.vnc_port = vnc_port
    cookie = scene.owner.cookies.get(SESSION_COOKIE)
    headers = {"Origin": ORIGIN, "Cookie": f"{SESSION_COOKIE}={cookie}"}
    # O dono abre a própria tela com ou sem o slot na URL.
    for url in ("/vnc/ws", f"/vnc/ws?slot={scene.slot_id}"):
        with scene.owner.websocket_connect(url, headers=headers) as ws:
            assert ws.receive_bytes() == b"RFB 003.008\n"
    for bad in ("abc", "999", "-1", "2"):
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            scene.owner.websocket_connect(f"/vnc/ws?slot={bad}", headers=headers),
        ):
            pass
        assert exc.value.code == 1008
    # Sem acesso, o próprio dono não ganha registro de "abriu" nos eventos.
    messages = [e["message"] for e in scene.owner.get("/api/events").json()["events"]]
    assert not any("abriu a tela remota" in m for m in messages)


def test_vnc_bridge_reevaluates_on_every_change(scene: Scene, world: World, vnc_port: int) -> None:
    runtime = world.slots.for_id(scene.slot_id)
    assert runtime is not None
    runtime.vnc_port = vnc_port
    cookie = scene.asker.cookies.get(SESSION_COOKIE)
    headers = {"Origin": ORIGIN, "Cookie": f"{SESSION_COOKIE}={cookie}"}
    open_to(scene.owner, scene.owner_csrf, ERUDHIR, "1h")
    open_to(scene.owner, scene.owner_csrf, "*", "sempre")
    granted = status(scene.owner)["access"]["granted"]
    ids = {g["audience"]: g["id"] for g in granted}
    with scene.asker.websocket_connect(f"/vnc/ws?slot={scene.slot_id}", headers=headers) as ws:
        assert ws.receive_bytes() == b"RFB 003.008\n"
        url = f"/api/access/grants/{ids[ERUDHIR]}"
        assert scene.owner.delete(url, headers=write(scene.owner_csrf)).status_code == 200
        threading.Event().wait(0.3)
        assert runtime.supervisor.viewers == 1  # ainda coberto pelo "para todos"
        url = f"/api/access/grants/{ids['todos']}"
        assert scene.owner.delete(url, headers=write(scene.owner_csrf)).status_code == 200
        with pytest.raises(WebSocketDisconnect) as ended:
            ws.receive_bytes()
        assert ended.value.code == 1008
