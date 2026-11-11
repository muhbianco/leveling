"""Acesso à tela remota da instância de outro jogador: pedido, link no WhatsApp, liberação pelo
dono logado, prazo, revogação e a ponte VNC respeitando tudo isso."""

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

from app.access import REQUEST_TTL_S, AccessService
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


def ask(client: TestClient, csrf: str, slot_id: int) -> Any:
    return client.post("/api/access/requests", json={"slot": slot_id}, headers=write(csrf))


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

    def request(self) -> tuple[str, str]:
        """Pede, espera o WhatsApp e devolve (id do pedido, token do link)."""
        res = ask(self.asker, self.asker_csrf, self.slot_id)
        assert res.status_code == 200, res.text
        access = wait_notified(self.asker, self.slot_id)
        assert access["state"] == "pendente"
        phone, text = self.world.messenger.sent[-1]
        assert phone == ACAIS_PHONE
        found = LINK.search(text)
        assert found is not None
        return access["request_id"], found.group(1)

    def approve(self, request_id: str, hours: int = 1) -> Any:
        return self.owner.post(
            f"/api/access/requests/{request_id}/approve",
            json={"hours": hours},
            headers=write(self.owner_csrf),
        )


@pytest.fixture
def scene(world: World, google: FakeGoogle, owner: TestClient, asker: TestClient) -> Scene:
    return Scene(world, google, owner, asker)


# --- pedido ------------------------------------------------------------------------------------


def test_request_sends_link_to_owner_whatsapp(scene: Scene, world: World) -> None:
    _request_id, token = scene.request()
    phone, text = world.messenger.sent[0]
    assert phone == ACAIS_PHONE
    assert ERUDHIR in text and "tela remota" in text
    assert shared_entry(scene.asker, scene.slot_id)["access"]["notify"] == "enviado"
    # No disco fica só o hash do token.
    stored = (world.slots._settings.data_dir / "access.json").read_text(encoding="utf-8")
    assert token not in stored
    # O pedido aparece no painel do dono também.
    incoming = status(scene.owner)["access"]["incoming"]
    assert [r["email"] for r in incoming] == [ERUDHIR]
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
    world.clock.now += REQUEST_TTL_S + 1
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
    assert body["csrf"]
    # Link encaminhado: quem pediu (ou qualquer outro) não consegue liberar.
    other = scene.asker.get(f"/api/access/link/{token}")
    assert (other.status_code, other.json()["detail"]) == (403, "outra_conta")
    stolen = scene.asker.post(
        f"/api/access/requests/{request_id}/approve",
        json={"hours": 168},
        headers=write(scene.asker_csrf),
    )
    assert (stolen.status_code, stolen.json()["detail"]) == (403, "outra_conta")
    assert scene.owner.get("/api/access/link/" + "x" * 32).status_code == 404
    assert scene.owner.get("/api/access/link/short").status_code == 422


# --- liberação -----------------------------------------------------------------------------------


def test_owner_approves_and_requester_opens_remote(
    scene: Scene, world: World, google: FakeGoogle, third: TestClient
) -> None:
    request_id, _token = scene.request()
    assert scene.approve(request_id, 12).status_code == 200
    granted = status(scene.owner)["access"]["granted"]
    assert [g["email"] for g in granted] == [ERUDHIR]
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


def test_approve_validates_hours(scene: Scene) -> None:
    request_id, _token = scene.request()
    assert scene.approve(request_id, 5).status_code == 422
    assert scene.approve(request_id, 1).status_code == 200
    assert scene.approve(request_id, 1).status_code == 404  # já respondido


def test_grant_expires(scene: Scene, world: World) -> None:
    request_id, _token = scene.request()
    scene.approve(request_id, 1)
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
    world.clock.now += REQUEST_TTL_S + 1
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


async def test_restart_marks_unfinished_notice_as_failed(settings: Settings) -> None:
    path = settings.data_dir / "access.json"
    path.parent.mkdir(parents=True)
    request = {
        "id": "a" * 12,
        "token_hash": "0" * 64,
        "requester": ERUDHIR,
        "owner": ACAIS,
        "slot_id": 1,
        "created_at": 1e12,
        "expires_at": 1e12 + REQUEST_TTL_S,
        "status": "pendente",
        "notify": "enviando",
    }
    path.write_text(json.dumps({"requests": [request], "grants": []}), encoding="utf-8")

    class NoSlots:
        def for_id(self, slot_id: int) -> None:
            return None

    service = AccessService(settings, NoSlots(), None, clock=lambda: 1e12)  # type: ignore[arg-type]
    await service.load()
    assert service._requests[0].notify == "falhou"


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
