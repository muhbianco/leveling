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
from app.policy import BlockKind, Observation, Screen
from app.slots import (
    AccountStore,
    SlotManager,
    SlotRecord,
    SlotRegistry,
    SlotRuntime,
    credentials_provider,
    decrypt_password,
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
    accounts = AccountStore(registry)
    drivers: dict[int, FakeDriver] = {}

    async def build(record: SlotRecord) -> SlotRuntime:
        driver = FakeDriver()
        drivers[record.id] = driver
        store = StateStore(registry.slot_dir(record.id) / "state.json")
        await store.load()
        sup = Supervisor(store, driver, credentials_provider(settings, accounts, record))
        return SlotRuntime(record, store, sup, driver, settings.vnc_port_for(record.id))

    slots = SlotManager(settings, registry, accounts, build)
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
    return add_account(client, csrf, username, password)


def add_account(
    client: TestClient, csrf: str, username: str, password: str = "pw"
) -> dict[str, Any]:
    res = client.post(
        "/api/accounts", json={"username": username, "password": password}, headers=write(csrf)
    )
    assert res.status_code == 200, res.text
    body: dict[str, Any] = res.json()
    return body


def accounts_of(body: dict[str, Any]) -> list[tuple[str, str, bool]]:
    return [(a["username"], a["source"], a["active"]) for a in body["slot"]["accounts"]]


def account_id(body: dict[str, Any], username: str) -> str:
    found: str = next(a["id"] for a in body["slot"]["accounts"] if a["username"] == username)
    return found


def active_name(body: dict[str, Any]) -> str | None:
    return next((a["username"] for a in body["slot"]["accounts"] if a["active"]), None)


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
    url = "/api/accounts"
    assert client.post(url, json=body, headers={"X-CSRF-Token": csrf}).status_code == 403
    evil = {"Origin": "https://evil.test", "X-CSRF-Token": csrf}
    assert client.post(url, json=body, headers=evil).status_code == 403
    assert client.post(url, json=body, headers=write("x")).status_code == 403
    assert client.get("/api/status").json()["slot"] is None


def test_player_creates_instance_and_turns_it_on(
    client: TestClient, google: FakeGoogle, world: World
) -> None:
    csrf = login(client, google, PLAYER)
    body = create_instance(client, csrf)
    assert body["slot"]["id"] == 1
    assert accounts_of(body) == [("hunter", "painel", True)]
    assert set(body["slot"]["accounts"][0]) == {"id", "username", "source", "active"}
    assert body["capacity"] == {"used": 1, "max": 2}
    assert body["desired_on"] is False

    on = client.post("/api/power", json={"on": True}, headers=write(csrf))
    assert on.status_code == 200
    assert on.json()["desired_on"] is True
    kinds = [e["kind"] for e in client.get("/api/events").json()["events"]]
    assert kinds[:3] == ["ligado", "conta", "criada"]

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
    stored = (world.registry.slot_dir(1) / "accounts.json").read_text(encoding="utf-8")
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
    assert (owner["slot"]["id"], active_name(owner), owner["desired_on"]) == (
        1,
        "owner-hunter",
        False,
    )
    assert (player["slot"]["id"], active_name(player), player["desired_on"]) == (
        2,
        "pug",
        True,
    )
    assert [a[0] for a in accounts_of(player)] == ["pug"]  # só o dono vê a conta do arquivo
    owner_events = [e["message"] for e in client.get("/api/events").json()["events"]]
    assert not any("pug" in m for m in owner_events)
    assert not any(e["kind"] == "ligado" for e in client.get("/api/events").json()["events"])


def test_third_player_gets_no_slot(
    client: TestClient, other: TestClient, google: FakeGoogle, world: World
) -> None:
    create_instance(client, login(client, google, ADMIN), username="owner-alt")
    create_instance(other, login(other, google, PLAYER), username="pug")
    with TestClient(world.app, base_url=ORIGIN, follow_redirects=False) as third:
        csrf = login(third, google, THIRD)
        assert third.get("/api/status").json()["capacity"] == {"used": 2, "max": 2}
        res = third.post(
            "/api/accounts",
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
    res = client.post(
        "/api/accounts", json={"username": "hunter", "password": "pw"}, headers=write(csrf)
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
        ("post", "/api/confirm/elsewhere", None),
        ("post", "/api/confirm/reconnect", None),
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
    res = client.post("/api/accounts", json=body, headers=write(csrf))
    assert res.status_code == 422


# --- contas do jogo -----------------------------------------------------------------------------


def test_player_registers_accounts_and_switches(
    client: TestClient, google: FakeGoogle, world: World
) -> None:
    csrf = login(client, google, PLAYER)
    create_instance(client, csrf, username="pug", password="pw-pug")
    body = add_account(client, csrf, "alt", "pw-alt")
    assert accounts_of(body) == [("pug", "painel", True), ("alt", "painel", False)]
    assert "pw-alt" not in json.dumps(body)
    alt = account_id(body, "alt")

    assert client.post("/api/power", json={"on": True}, headers=write(csrf)).status_code == 200
    driver = world.drivers[1]
    driver.calls.clear()

    res = client.post(f"/api/accounts/{alt}/activate", headers=write(csrf))
    assert res.status_code == 200, res.text
    switched = res.json()
    assert accounts_of(switched) == [("pug", "painel", False), ("alt", "painel", True)]
    assert switched["desired_on"] is True  # a chave fica como estava
    assert driver.calls == ["forget"]  # navegador fechado e perfil (token) apagado
    messages = [e["message"] for e in client.get("/api/events").json()["events"]]
    assert messages[0] == "Conta ativa agora: alt"

    runtime = world.slots.for_email(PLAYER)
    assert runtime is not None
    asyncio.run(runtime.supervisor.tick())
    assert runtime.supervisor.status().game_username == "alt"
    assert driver.calls[-1] == "launch"  # ligada: reabre já na conta nova

    # Ativar a que já está ativa não fecha nada.
    driver.calls.clear()
    assert client.post(f"/api/accounts/{alt}/activate", headers=write(csrf)).status_code == 200
    assert driver.calls == []


def test_saving_a_listed_hunter_changes_its_password(
    client: TestClient, google: FakeGoogle, world: World
) -> None:
    csrf = login(client, google, PLAYER)
    create_instance(client, csrf, username="pug", password="velha")
    body = add_account(client, csrf, "PUG", "nova")
    assert accounts_of(body) == [("PUG", "painel", True)]
    book = AccountStore(world.registry).load(1)
    assert decrypt_password(book.accounts[0], DATA_KEY) == "nova"
    messages = [e["message"] for e in client.get("/api/events").json()["events"]]
    assert messages[0] == "Conta PUG com senha nova"


def test_owner_sees_the_file_account_and_players_cannot_take_it(
    client: TestClient, other: TestClient, google: FakeGoogle, world: World
) -> None:
    owner_csrf = login(client, google, ADMIN)
    body = create_instance(client, owner_csrf, username="owner-alt")
    # Criar a instância ativa a conta digitada; a do arquivo fica na lista, primeiro.
    assert accounts_of(body) == [
        ("owner-hunter", "servidor", False),
        ("owner-alt", "painel", True),
    ]
    res = client.post("/api/accounts/servidor/activate", headers=write(owner_csrf))
    assert res.status_code == 200
    assert accounts_of(res.json()) == [
        ("owner-hunter", "servidor", True),
        ("owner-alt", "painel", False),
    ]

    player_csrf = login(other, google, PLAYER)
    refused = other.post(
        "/api/accounts",
        json={"username": "Owner-Hunter", "password": "x"},
        headers=write(player_csrf),
    )
    assert (refused.status_code, refused.json()["detail"]) == (409, "conta_em_uso")
    create_instance(other, player_csrf, username="pug")
    server = other.post("/api/accounts/servidor/activate", headers=write(player_csrf))
    assert server.status_code == 404  # a conta do arquivo não existe para o jogador


def test_same_account_is_active_in_one_slot_only(
    client: TestClient, other: TestClient, google: FakeGoogle, world: World
) -> None:
    owner_csrf = login(client, google, ADMIN)
    player_csrf = login(other, google, PLAYER)
    create_instance(client, owner_csrf, username="Shared")

    # Criar a instância com a conta ativa da outra vaga: recusa sem ocupar vaga.
    res = other.post(
        "/api/accounts", json={"username": "shared", "password": "pw"}, headers=write(player_csrf)
    )
    assert (res.status_code, res.json()["detail"]) == (409, "conta_em_uso")
    assert other.get("/api/status").json()["slot"] is None
    assert world.registry.by_email(PLAYER) is None

    # Cadastrar pode; ativar não.
    create_instance(other, player_csrf, username="pug")
    body = add_account(other, player_csrf, "SHARED")
    assert accounts_of(body) == [("pug", "painel", True), ("SHARED", "painel", False)]
    shared = account_id(body, "SHARED")
    refused = other.post(f"/api/accounts/{shared}/activate", headers=write(player_csrf))
    assert (refused.status_code, refused.json()["detail"]) == (409, "conta_em_uso")
    assert world.drivers[2].calls == []  # nada fechado

    # O dono troca de conta; aí a compartilhada fica livre.
    client.post("/api/accounts/servidor/activate", headers=write(owner_csrf))
    ok = other.post(f"/api/accounts/{shared}/activate", headers=write(player_csrf))
    assert ok.status_code == 200
    assert active_name(ok.json()) == "SHARED"
    back = client.post(
        f"/api/accounts/{account_id(client.get('/api/status').json(), 'Shared')}/activate",
        headers=write(owner_csrf),
    )
    assert (back.status_code, back.json()["detail"]) == (409, "conta_em_uso")


def test_account_removal_rules(client: TestClient, google: FakeGoogle) -> None:
    csrf = login(client, google, ADMIN)
    create_instance(client, csrf, username="owner-alt")
    body = add_account(client, csrf, "outra")
    alt, outra = account_id(body, "owner-alt"), account_id(body, "outra")
    for target, code, detail in [
        (alt, 409, "conta_ativa"),
        ("servidor", 409, "conta_do_servidor"),
        ("0123456789ab", 404, "conta_nao_encontrada"),
    ]:
        res = client.delete(f"/api/accounts/{target}", headers=write(csrf))
        assert (res.status_code, res.json()["detail"]) == (code, detail)
    assert client.delete("/api/accounts/NAO-E-ID", headers=write(csrf)).status_code == 422
    res = client.delete(f"/api/accounts/{outra}", headers=write(csrf))
    assert res.status_code == 200
    assert [a[0] for a in accounts_of(res.json())] == ["owner-hunter", "owner-alt"]
    messages = [e["message"] for e in client.get("/api/events").json()["events"]]
    assert messages[0] == "Conta outra removida"


def test_account_ids_are_scoped_to_the_session(
    client: TestClient, other: TestClient, google: FakeGoogle
) -> None:
    owner_csrf = login(client, google, ADMIN)
    create_instance(client, owner_csrf, username="owner-alt")
    extra = account_id(add_account(client, owner_csrf, "owner-extra"), "owner-extra")
    player_csrf = login(other, google, PLAYER)
    create_instance(other, player_csrf, username="pug")

    assert (
        other.post(f"/api/accounts/{extra}/activate", headers=write(player_csrf)).status_code == 404
    )
    assert other.delete(f"/api/accounts/{extra}", headers=write(player_csrf)).status_code == 404
    owner = client.get("/api/status").json()
    assert [a[0] for a in accounts_of(owner)] == ["owner-hunter", "owner-alt", "owner-extra"]


def test_failed_switch_keeps_the_account(
    client: TestClient, google: FakeGoogle, world: World
) -> None:
    csrf = login(client, google, PLAYER)
    create_instance(client, csrf, username="pug")
    alt = account_id(add_account(client, csrf, "alt"), "alt")

    async def broken() -> None:
        raise OSError("profile busy")

    world.drivers[1].forget_session = broken  # type: ignore[method-assign]
    res = client.post(f"/api/accounts/{alt}/activate", headers=write(csrf))
    assert (res.status_code, res.json()["detail"]) == (503, "troca_falhou")
    assert active_name(client.get("/api/status").json()) == "pug"
    events = client.get("/api/events").json()["events"]
    assert (events[0]["kind"], events[0]["message"]) == (
        "erro",
        "Troca para alt falhou: a conta anterior continua",
    )


# --- confirmação antes de entrar de novo -----------------------------------------------------


def open_confirmation(client: TestClient, google: FakeGoogle, world: World) -> str:
    """Jogador liga, o robô chega a jogar e a sessão cai com "Sessão expirada"."""
    csrf = login(client, google, PLAYER)
    create_instance(client, csrf, username="pug")
    assert client.post("/api/power", json={"on": True}, headers=write(csrf)).status_code == 200
    runtime = world.slots.for_email(PLAYER)
    assert runtime is not None
    driver = world.drivers[1]

    async def play_then_expire() -> None:
        await runtime.supervisor.tick()  # abre
        driver.obs = Observation(Screen.GAME, signal_age=1.0, screen_age=600.0)
        await runtime.supervisor.tick()
        driver.obs = Observation(
            Screen.BLOCK, block_kind=BlockKind.RELOGIN, block_text="Sessão expirada"
        )
        await runtime.supervisor.tick()

    asyncio.run(play_then_expire())
    return csrf


def test_status_shows_the_confirmation_window(
    client: TestClient, google: FakeGoogle, world: World
) -> None:
    open_confirmation(client, google, world)
    body = client.get("/api/status").json()
    assert body["phase"] == "confirmando"
    assert 0 < body["confirm_in"] <= 15
    assert world.drivers[1].logins == []


def test_confirm_elsewhere_turns_off_without_logging_in(
    client: TestClient, google: FakeGoogle, world: World
) -> None:
    csrf = open_confirmation(client, google, world)
    res = client.post("/api/confirm/elsewhere", headers=write(csrf))
    assert res.status_code == 200
    body = res.json()
    assert (body["desired_on"], body["off_reason"], body["confirm_in"]) == (
        False,
        "assumido",
        None,
    )
    assert world.drivers[1].logins == []


def test_confirm_reconnect_skips_the_wait(
    client: TestClient, google: FakeGoogle, world: World
) -> None:
    csrf = open_confirmation(client, google, world)
    res = client.post("/api/confirm/reconnect", headers=write(csrf))
    assert res.status_code == 200
    assert res.json()["confirm_in"] is None
    again = client.post("/api/confirm/reconnect", headers=write(csrf))
    assert (again.status_code, again.json()["detail"]) == (409, "sem_confirmacao")


def test_account_routes_without_instance(client: TestClient, google: FakeGoogle) -> None:
    csrf = login(client, google, PLAYER)
    res = client.post("/api/accounts/0123456789ab/activate", headers=write(csrf))
    assert (res.status_code, res.json()["detail"]) == (409, "sem_instancia")
    res = client.delete("/api/accounts/0123456789ab", headers=write(csrf))
    assert (res.status_code, res.json()["detail"]) == (409, "sem_instancia")


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
