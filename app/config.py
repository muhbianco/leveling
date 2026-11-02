"""Configuração. O Env da stack traz o que não é segredo; os segredos vêm de um arquivo montado
read-only (/run/leveling/secrets.env), relido a cada uso para que trocar a senha não peça redeploy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from urllib.parse import urlsplit

from pydantic_settings import BaseSettings, SettingsConfigDict

GOOGLE_ISSUER = "https://accounts.google.com"
SECRET_KEYS = ("GAME_USERNAME", "GAME_PASSWORD", "GOOGLE_CLIENT_SECRET")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    log_level: str = "INFO"
    public_url: str = "https://leveling.muhbianco.com.br"
    # E-mails Google que entram no painel (vírgula). Vazio = ninguém entra.
    admin_emails: str = ""
    google_client_id: str = ""
    google_oidc_authorize_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    google_oidc_token_url: str = "https://oauth2.googleapis.com/token"  # noqa: S105 (a URL)
    google_oidc_jwks_url: str = "https://www.googleapis.com/oauth2/v3/certs"
    google_oidc_timeout_seconds: float = 10.0

    secrets_file: Path = Path("/run/leveling/secrets.env")
    data_dir: Path = Path("/data")

    game_url: str = "https://levelingidle2d.com.br/jogar"
    # Xvfb + x11vnc são do container; no desenvolvimento ficam desligados.
    manage_display: bool = True
    display: str = ":99"
    screen_width: int = 1280
    screen_height: int = 720
    vnc_host: str = "127.0.0.1"
    vnc_port: int = 5900
    novnc_dir: Path = Path("/usr/share/novnc")
    vnc_max_clients: int = 2

    tick_seconds: float = 5.0

    @cached_property
    def admin_email_set(self) -> frozenset[str]:
        return frozenset(e.strip().lower() for e in self.admin_emails.split(",") if e.strip())

    @property
    def public_origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_origin}/auth/callback"

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "profile"


@dataclass(frozen=True)
class Secrets:
    game_username: str = ""
    game_password: str = field(default="", repr=False)
    google_client_secret: str = field(default="", repr=False)
    # mtime do arquivo: o supervisor libera o login travado quando o dono troca a senha.
    version: float = 0.0

    @property
    def has_game_credentials(self) -> bool:
        return bool(self.game_username and self.game_password)


def _read_env_file(path: Path) -> dict[str, str]:
    """`CHAVE=valor` cru, como o install-secrets.sh grava: a senha vale byte a byte (sem aparar
    espaços nem aspas)."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        values[key.strip()] = value
    return values


def read_secrets(path: Path) -> Secrets:
    """Arquivo montado em produção; variáveis de ambiente no desenvolvimento. Nunca loga valores."""
    values = {key: os.environ.get(key, "") for key in SECRET_KEYS}
    version = 0.0
    try:
        version = path.stat().st_mtime
        file_values = _read_env_file(path)
    except FileNotFoundError:
        file_values = {}
    for key in SECRET_KEYS:
        if file_values.get(key):
            values[key] = file_values[key]
    return Secrets(
        game_username=values["GAME_USERNAME"].strip(),
        game_password=values["GAME_PASSWORD"],
        google_client_secret=values["GOOGLE_CLIENT_SECRET"].strip(),
        version=version,
    )
