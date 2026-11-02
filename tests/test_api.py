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
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from starlette.websockets import WebSocketDisconnect

from app.api import Services
from app.auth import SESSION_COOKIE, AuthService
from app.config import Settings
from app.main import create_app
from app.oidc import OidcClient
from app.state import StateStore
from app.supervisor import Supervisor
from tests.test_supervisor import FakeDriver

ORIGIN = "https://leveling.test"
ADMIN = "mear.mind@gmail.com"
CLIENT_ID = "client-123"


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


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    secrets = tmp_path / "secrets.env"
    secrets.write_text(
        "GAME_USERNAME=hunter\nGAME_PASSWORD=pw\nGOOGLE_CLIENT_SECRET=google-secret\n",
        encoding="utf-8",
    )
    novnc = tmp_path / "novnc"
    novnc.mkdir()
    (novnc / "vnc.html").write_text("<html>novnc</html>", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    return Settings(
        public_url=ORIGIN,
        admin_emails=ADMIN,
        google_client_id=CLIENT_ID,
        google_oidc_token_url="https://oauth2.googleapis.com/token",
        google_oidc_jwks_url="https://www.googleapis.com/oauth2/v3/certs",
        secrets_file=secrets,
        data_dir=tmp_path / "data",
        manage_display=False,
        novnc_dir=novnc,
    )


@pytest.fixture
def client(settings: Settings, google: FakeGoogle) -> Iterator[TestClient]:
    driver = FakeDriver()
    store = StateStore(settings.data_dir / "state.json")
    supervisor = Supervisor(store, driver, settings.secrets_file)
    auth = AuthService(
        settings, OidcClient(settings, transport=httpx.MockTransport(google.handler))
    )
    app = create_app(settings, Services(store, supervisor, auth, driver))
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as c:
        yield c


def start_login(client: TestClient) -> dict[str, str]:
    res = client.get("/auth/login")
    assert res.status_code == 303
    query = parse_qs(urlsplit(res.headers["location"]).query)
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [f"{ORIGIN}/auth/callback"]
    assert query["code_challenge_method"] == ["S256"]
    return {"state": query["state"][0], "nonce": query["nonce"][0]}


def login(client: TestClient, google: FakeGoogle) -> str:
    flow = start_login(client)
    google.claims = {"nonce": flow["nonce"]}
    res = client.get("/auth/callback", params={"state": flow["state"], "code": "abc"})
    assert res.status_code == 303, res.text
    assert res.headers["location"] == "/"
    status = client.get("/api/status")
    assert status.status_code == 200
    csrf: str = status.json()["csrf"]
    return csrf


def test_healthz_is_open(client: TestClient) -> None:
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json()["ok"] is True


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


def test_google_login_creates_session(client: TestClient, google: FakeGoogle) -> None:
    login(client, google)
    body = client.get("/api/status").json()
    assert body["email"] == ADMIN
    assert body["desired_on"] is False
    assert "Leveling Keeper" in client.get("/").text
    assert "Eventos" in client.get("/").text


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
    assert (
        client.get("/auth/callback", params={"state": flow["state"], "code": "abc"}).status_code
        == 303
    )
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


def test_writes_need_origin_and_csrf(client: TestClient, google: FakeGoogle) -> None:
    csrf = login(client, google)
    body = {"on": True}
    assert client.post("/api/power", json=body, headers={"X-CSRF-Token": csrf}).status_code == 403
    assert (
        client.post(
            "/api/power", json=body, headers={"Origin": "https://evil.test", "X-CSRF-Token": csrf}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/power", json=body, headers={"Origin": ORIGIN, "X-CSRF-Token": "x"}
        ).status_code
        == 403
    )
    ok = client.post("/api/power", json=body, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
    assert ok.status_code == 200
    assert ok.json()["desired_on"] is True
    events = client.get("/api/events").json()["events"]
    assert events[0]["kind"] == "ligado"
    assert events[0]["at"].endswith("Z")


def test_reload_when_off_conflicts(client: TestClient, google: FakeGoogle) -> None:
    csrf = login(client, google)
    res = client.post("/api/reload", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
    assert res.status_code == 409


def test_events_limit_is_bounded(client: TestClient, google: FakeGoogle) -> None:
    login(client, google)
    assert client.get("/api/events", params={"limit": 1000}).status_code == 422


def test_screenshot_without_browser(client: TestClient, google: FakeGoogle) -> None:
    login(client, google)
    assert client.get("/api/screenshot").status_code == 409
    assert client.get("/api/screenshot").status_code == 429  # 1 a cada 2 s


def test_logout_ends_session(client: TestClient, google: FakeGoogle) -> None:
    csrf = login(client, google)
    res = client.post("/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
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
    login(client, google)
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


def test_vnc_socket_rejects_foreign_origin(client: TestClient, google: FakeGoogle) -> None:
    login(client, google)
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/vnc/ws", headers={"Origin": "https://evil.test"}),
    ):
        pass
    assert exc.value.code == 1008
