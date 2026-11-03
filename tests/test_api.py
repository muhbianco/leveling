from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from starlette.websockets import WebSocketDisconnect

from app.api import Services
from app.auth import SESSION_COOKIE, AuthService
from app.config import Settings
from app.main import create_app
from app.oidc import OidcClient
from app.slots import (
    CredentialStore,
    SlotManager,
    SlotRecord,
    SlotRegistry,
    SlotRuntime,
    credentials_provider,
)
from app.state import StateStore
from app.supervisor import Supervisor
from tests.test_supervisor import FakeDriver

ORIGIN = "https://leveling.test"
ADMIN = "mear.mind@gmail.com"
PLAYER = "pugtimothy@gmail.com"
THIRD = "third@example.com"
CLIENT_ID = "client-123"
DATA_KEY = Fernet.generate_key().decode()


@dataclass
class FakeGoogle:
    """Token endpoint + JWKS do Google, na borda (httpx.MockTransport)."""

    key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    claims: dict[str, Any] = field(default_factory=dict)
    token_calls: int = 0

    def id_token(self) -> str:
        now = int(time.time())
        base = {
            "iss": "https://accounts.google.com",
            "aud": CLIENT_ID,
            "sub": "1234",
            "iat": now,
            "exp": now + 300,
            "email": ADMIN,
            "email_verified": True,
        }
        return jwt.encode(
            {**base, **self.claims}, self.key, algorithm="RS256", headers={"kid": "k1"}
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            self.token_calls += 1
            form = parse_qs(request.content.decode())
            assert form["client_secret"] == ["google-secret"]
            assert form["code_verifier"][0]
            return httpx.Response(200, json={"id_token": self.id_token()})
        if request.url.path == "/oauth2/v3/certs":
            jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
            jwk.update({"kid": "k1", "alg": "RS256", "use": "sig"})
            return httpx.Response(
                200, json={"keys": [jwk]}, headers={"cache-control": "max-age=600"}
            )
        return httpx.Response(404)


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


def write_secrets(path: Path, data_key: str = DATA_KEY) -> None:
    path.write_text(
        "GAME_USERNAME=owner-hunter\nGAME_PASSWORD=owner-pw\nGOOGLE_CLIENT_SECRET=google-secret\n"
        f"DATA_KEY={data_key}\n",
        encoding="utf-8",
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    secrets = tmp_path / "secrets.env"
    write_secrets(secrets)
    novnc = tmp_path / "novnc"
    novnc.mkdir()
    (novnc / "vnc.html").write_text("<html>novnc</html>", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    return Settings(
        public_url=ORIGIN,
        admin_emails=ADMIN,
        player_emails=f"{PLAYER},{THIRD}",
        google_client_id=CLIENT_ID,
        google_oidc_token_url="https://oauth2.googleapis.com/token",
        google_oidc_jwks_url="https://www.googleapis.com/oauth2/v3/certs",
        secrets_file=secrets,
        data_dir=tmp_path / "data",
        manage_display=False,
        novnc_dir=novnc,
    )


@dataclass
class World:
    app: FastAPI
    slots: SlotManager
    registry: SlotRegistry
    drivers: dict[int, FakeDriver]


@pytest.fixture
def world(settings: Settings, google: FakeGoogle) -> World:
    registry = SlotRegistry(settings.data_dir, settings.max_slots)
    creds = CredentialStore(registry)
    drivers: dict[int, FakeDriver] = {}

    async def build(record: SlotRecord) -> SlotRuntime:
        driver = FakeDriver()
        drivers[record.id] = driver
        store = StateStore(registry.slot_dir(record.id) / "state.json")
        await store.load()
        sup = Supervisor(store, driver, credentials_provider(settings, creds, record))
        return SlotRuntime(record, store, sup, driver, settings.vnc_port_for(record.id))

    slots = SlotManager(settings, registry, creds, build)
    asyncio.run(slots.start())
    auth = AuthService(
        settings, OidcClient(settings, transport=httpx.MockTransport(google.handler))
    )
    app = create_app(settings, Services(auth, slots))
    return World(app, slots, registry, drivers)


@pytest.fixture
def client(world: World) -> Iterator[TestClient]:
    with TestClient(world.app, base_url=ORIGIN, follow_redirects=False) as c:
        yield c


@pytest.fixture
def other(world: World) -> Iterator[TestClient]:
    """Segundo navegador (outro jogador), com cookies próprios."""
    with TestClient(world.app, base_url=ORIGIN, follow_redirects=False) as c:
        yield c


def start_login(client: TestClient) -> dict[str, str]:
    res = client.get("/auth/login")
    assert res.status_code == 303
    query = parse_qs(urlsplit(res.headers["location"]).query)
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [f"{ORIGIN}/auth/callback"]
    assert query["code_challenge_method"] == ["S256"]
    return {"state": query["state"][0], "nonce": query["nonce"][0]}


def login(client: TestClient, google: FakeGoogle, email: str = ADMIN) -> str:
    flow = start_login(client)
    google.claims = {"nonce": flow["nonce"], "email": email}
    res = client.get("/auth/callback", params={"state": flow["state"], "code": "abc"})
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/"
    status = client.get("/api/status")
    assert status.status_code == 200
    csrf: str = status.json()["csrf"]
    return csrf


def write(csrf: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf}


def create_instance(
    client: TestClient, csrf: str, username: str = "hunter", password: str = "s3nh@ 'x'"
) -> dict[str, Any]:
    res = client.put(
        "/api/slot/credentials",
        json={"username": username, "password": password},
        headers=write(csrf),
    )
    assert res.status_code == 200, res.text
    body: dict[str, Any] = res.json()
    return body


# --- abertas e login ----------------------------------------------------------------------------


def test_healthz_is_open(client: TestClient) -> None:
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"ok": True, "slots": 0}


def test_index_without_session_is_login_page(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "Entrar com Google" in res.text
    assert "frame-ancestors 'none'" in res.headers["content-security-policy"]


@pytest.mark.parametrize(
    "path", ["/api/status", "/api/events", "/api/screenshot", "/vnc/vnc.html", "/vnc/"]
)
def test_reads_require_session(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401


def test_google_login_without_instance_offers_setup(client: TestClient, google: FakeGoogle) -> None:
    login(client, google, PLAYER)
    body = client.get("/api/status").json()
    assert body["email"] == PLAYER
    assert body["slot"] is None
    assert body["capacity"] == {"used": 0, "max": 2}
    assert "desired_on" not in body
    assert "Criar minha instância" in client.get("/").text


@pytest.mark.parametrize(
    ("claims", "code"),
    [
        ({"email": "someone@gmail.com"}, "conta_nao_autorizada"),
        ({"email_verified": False}, "email_nao_verificado"),
        ({"aud": "other-client"}, "login_invalido"),
    ],
)
def test_login_refused(
    client: TestClient, google: FakeGoogle, claims: dict[str, Any], code: str
) -> None:
    flow = start_login(client)
    google.claims = {"nonce": flow["nonce"], **claims}
    res = client.get("/auth/callback", params={"state": flow["state"], "code": "abc"})
    assert res.status_code == 403
    assert SESSION_COOKIE not in res.cookies
    assert client.get("/api/status").status_code == 401


def test_nonce_mismatch_refused(client: TestClient, google: FakeGoogle) -> None:
    flow = start_login(client)
    google.claims = {"nonce": "not-the-flow-nonce"}
    res = client.get("/auth/callback", params={"state": flow["state"], "code": "abc"})
    assert res.status_code == 403


def test_state_is_single_use(client: TestClient, google: FakeGoogle) -> None:
    flow = start_login(client)
    google.claims = {"nonce": flow["nonce"]}
    first = client.get("/auth/callback", params={"state": flow["state"], "code": "abc"})
    assert first.status_code == 303
    again = client.get("/auth/callback", params={"state": flow["state"], "code": "abc"})
    assert again.status_code == 400
    assert google.token_calls == 1


def test_callback_needs_flow_cookie_from_same_browser(
    client: TestClient, google: FakeGoogle
) -> None:
    flow = start_login(client)
    client.cookies.clear()
    res = client.get("/auth/callback", params={"state": flow["state"], "code": "abc"})
    assert res.status_code == 400
    assert google.token_calls == 0


# --- instância do jogador -----------------------------------------------------------------------


def test_writes_need_origin_and_csrf(client: TestClient, google: FakeGoogle) -> None:
    csrf = login(client, google, PLAYER)
    body = {"username": "hunter", "password": "pw"}
    url = "/api/slot/credentials"
    assert client.put(url, json=body, headers={"X-CSRF-Token": csrf}).status_code == 403
    evil = {"Origin": "https://evil.test", "X-CSRF-Token": csrf}
    assert client.put(url, json=body, headers=evil).status_code == 403
    assert client.put(url, json=body, headers=write("x")).status_code == 403
    assert client.get("/api/status").json()["slot"] is None


def test_player_creates_instance_and_turns_it_on(
    client: TestClient, google: FakeGoogle, world: World
) -> None:
    csrf = login(client, google, PLAYER)
    body = create_instance(client, csrf)
    assert body["slot"] == {"id": 1, "username": "hunter", "credentials": "painel"}
    assert body["capacity"] == {"used": 1, "max": 2}
    assert body["desired_on"] is False

    on = client.post("/api/power", json={"on": True}, headers=write(csrf))
    assert on.status_code == 200
    assert on.json()["desired_on"] is True
    kinds = [e["kind"] for e in client.get("/api/events").json()["events"]]
    assert kinds[:3] == ["ligado", "login", "criada"]

    # O supervisor do slot entra com a senha cadastrada.
    runtime = world.slots.for_email(PLAYER)
    assert runtime is not None
    status = runtime.supervisor.status()
    assert status.has_credentials is False  # ainda não rodou nenhum ciclo
    asyncio.run(runtime.supervisor.tick())
    assert runtime.supervisor.status().game_username == "hunter"


def test_password_is_encrypted_and_never_returned(
    client: TestClient, google: FakeGoogle, world: World
) -> None:
    csrf = login(client, google, PLAYER)
    body = create_instance(client, csrf, password="segredo-do-jogo")
    assert "segredo-do-jogo" not in json.dumps(body)
    stored = (world.registry.slot_dir(1) / "credentials.json").read_text(encoding="utf-8")
    assert "segredo-do-jogo" not in stored
    assert "hunter" in stored


def test_players_are_isolated(
    client: TestClient, other: TestClient, google: FakeGoogle, world: World
) -> None:
    owner_csrf = login(client, google, ADMIN)
    player_csrf = login(other, google, PLAYER)
    create_instance(client, owner_csrf, username="owner-hunter")
    create_instance(other, player_csrf, username="pug")

    assert (
        other.post("/api/power", json={"on": True}, headers=write(player_csrf)).status_code == 200
    )

    owner = client.get("/api/status").json()
    player = other.get("/api/status").json()
    assert (owner["slot"]["id"], owner["slot"]["username"], owner["desired_on"]) == (
        1,
        "owner-hunter",
        False,
    )
    assert (player["slot"]["id"], player["slot"]["username"], player["desired_on"]) == (
        2,
        "pug",
        True,
    )
    owner_events = [e["message"] for e in client.get("/api/events").json()["events"]]
    assert not any("pug" in m for m in owner_events)
    assert not any(e["kind"] == "ligado" for e in client.get("/api/events").json()["events"])


def test_third_player_gets_no_slot(
    client: TestClient, other: TestClient, google: FakeGoogle, world: World
) -> None:
    create_instance(client, login(client, google, ADMIN))
    create_instance(other, login(other, google, PLAYER))
    with TestClient(world.app, base_url=ORIGIN, follow_redirects=False) as third:
        csrf = login(third, google, THIRD)
        assert third.get("/api/status").json()["capacity"] == {"used": 2, "max": 2}
        res = third.put(
            "/api/slot/credentials",
            json={"username": "third", "password": "pw"},
            headers=write(csrf),
        )
        assert res.status_code == 409
        assert res.json()["detail"] == "sem_vaga"


def test_missing_data_key_creates_nothing(
    client: TestClient, google: FakeGoogle, settings: Settings, world: World
) -> None:
    write_secrets(settings.secrets_file, data_key="")
    csrf = login(client, google, PLAYER)
    res = client.put(
        "/api/slot/credentials", json={"username": "hunter", "password": "pw"}, headers=write(csrf)
    )
    assert res.status_code == 503
    assert res.json()["detail"] == "sem_chave"
    assert world.slots.used == 0


def test_delete_frees_the_slot(client: TestClient, google: FakeGoogle, world: World) -> None:
    csrf = login(client, google, PLAYER)
    create_instance(client, csrf)
    slot_dir = world.registry.slot_dir(1)
    assert slot_dir.exists()
    res = client.delete("/api/slot", headers=write(csrf))
    assert res.status_code == 200
    assert res.json()["slot"] is None
    assert res.json()["capacity"] == {"used": 0, "max": 2}
    assert world.drivers[1].calls[-1] == "close"
    assert not slot_dir.exists()
    assert client.delete("/api/slot", headers=write(csrf)).status_code == 409


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/power", {"on": True}),
        ("post", "/api/reload", None),
        ("post", "/api/retry-login", None),
    ],
)
def test_actions_without_instance_conflict(
    client: TestClient, google: FakeGoogle, method: str, path: str, body: dict[str, Any] | None
) -> None:
    csrf = login(client, google, PLAYER)
    res = getattr(client, method)(path, json=body, headers=write(csrf))
    assert res.status_code == 409
    assert res.json()["detail"] == "sem_instancia"
    assert client.get("/api/screenshot").status_code == 409
    assert client.get("/vnc/").status_code == 409
    assert client.get("/api/events").json() == {"events": []}


@pytest.mark.parametrize(
    "body",
    [
        {"username": "dois nomes", "password": "pw"},
        {"username": "ab", "password": "pw"},
        {"username": "hunter", "password": "linha\nquebrada"},
        {"username": "hunter", "password": ""},
        {"username": "hunter", "password": "x" * 129},
    ],
)
def test_credentials_are_validated(
    client: TestClient, google: FakeGoogle, body: dict[str, str]
) -> None:
    csrf = login(client, google, PLAYER)
    res = client.put("/api/slot/credentials", json=body, headers=write(csrf))
    assert res.status_code == 422


# --- demais rotas -------------------------------------------------------------------------------


def test_reload_when_off_conflicts(client: TestClient, google: FakeGoogle) -> None:
    csrf = login(client, google)
    create_instance(client, csrf)
    res = client.post("/api/reload", headers=write(csrf))
    assert res.status_code == 409


def test_events_limit_is_bounded(client: TestClient, google: FakeGoogle) -> None:
    login(client, google)
    assert client.get("/api/events", params={"limit": 1000}).status_code == 422


def test_screenshot_without_browser(client: TestClient, google: FakeGoogle) -> None:
    csrf = login(client, google)
    create_instance(client, csrf)
    assert client.get("/api/screenshot").status_code == 409
    assert client.get("/api/screenshot").status_code == 429  # 1 a cada 2 s


def test_logout_ends_session(client: TestClient, google: FakeGoogle) -> None:
    csrf = login(client, google)
    res = client.post("/auth/logout", headers=write(csrf))
    assert res.status_code == 204
    assert client.get("/api/status").status_code == 401


def test_session_survives_restart(
    settings: Settings, client: TestClient, google: FakeGoogle
) -> None:
    login(client, google)
    token = client.cookies.get(SESSION_COOKIE)
    fresh = AuthService(settings, OidcClient(settings))
    asyncio.run(fresh.load())
    assert fresh.session_for(token) is not None
    stored = json.loads((settings.data_dir / "sessions.json").read_text(encoding="utf-8"))
    assert token not in json.dumps(stored)  # só o hash vai para o disco


def test_novnc_files_are_served_inside_the_folder_only(
    client: TestClient, google: FakeGoogle
) -> None:
    csrf = login(client, google)
    create_instance(client, csrf)
    assert client.get("/vnc/vnc.html").text == "<html>novnc</html>"
    assert client.get("/vnc/../outside.txt").status_code == 404
    assert client.get("/vnc/%2e%2e/outside.txt").status_code == 404
    home = client.get("/vnc/")
    assert home.status_code == 303
    assert "path=vnc%2Fws" in home.headers["location"]


def test_vnc_socket_needs_session(client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/vnc/ws", headers={"Origin": ORIGIN}),
    ):
        pass
    assert exc.value.code == 1008


def test_vnc_socket_needs_an_instance(client: TestClient, google: FakeGoogle) -> None:
    login(client, google, PLAYER)
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/vnc/ws", headers={"Origin": ORIGIN}),
    ):
        pass
    assert exc.value.code == 1008


def test_vnc_socket_rejects_foreign_origin(client: TestClient, google: FakeGoogle) -> None:
    csrf = login(client, google)
    create_instance(client, csrf)
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/vnc/ws", headers={"Origin": "https://evil.test"}),
    ):
        pass
    assert exc.value.code == 1008
