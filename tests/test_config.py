from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, read_secrets


def test_password_is_kept_byte_for_byte(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"
    path.write_text(
        '# comentário\nGAME_USERNAME=hunter\nGAME_PASSWORD= p@ss="x" \nGOOGLE_CLIENT_SECRET=g\n',
        encoding="utf-8",
    )
    secrets = read_secrets(path)
    assert secrets.game_username == "hunter"
    assert secrets.game_password == ' p@ss="x" '
    assert secrets.google_client_secret == "g"
    assert secrets.has_game_credentials
    assert "p@ss" not in repr(secrets)


def test_missing_file_means_no_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("GAME_USERNAME", "GAME_PASSWORD", "GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    secrets = read_secrets(tmp_path / "nope.env")
    assert not secrets.has_game_credentials
    assert secrets.version == 0.0


def test_admin_emails_and_origin() -> None:
    s = Settings(admin_emails=" Mear.Mind@gmail.com , ", public_url="https://leveling.test/x")
    assert s.admin_email_set == frozenset({"mear.mind@gmail.com"})
    assert s.public_origin == "https://leveling.test"
    assert s.redirect_uri == "https://leveling.test/auth/callback"
    assert Settings(admin_emails="").admin_email_set == frozenset()


def test_owner_is_the_first_admin() -> None:
    s = Settings(admin_emails="Mear.Mind@gmail.com, pug@x.com")
    assert s.owner_email == "mear.mind@gmail.com"
    assert s.admin_email_set == frozenset({"mear.mind@gmail.com", "pug@x.com"})
    assert Settings(admin_emails="").owner_email is None


def test_player_phones() -> None:
    s = Settings(
        player_phones=(
            "Pug@x.com=+55 12 98888-7777, gab@x.com=(34) 9123-4567,"
            " ruim@x.com=123, semigual@x.com, =5511999990000"
        )
    )
    assert s.phone_map == {"pug@x.com": "5512988887777", "gab@x.com": "553491234567"}
    assert s.phone_for("ruim@x.com") is None


def test_wuzapi_comes_from_the_secrets_file(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"
    path.write_text("WUZAPI_BASE_URL=http://w:8080\nWUZAPI_SESSION_TOKEN=tok\n", encoding="utf-8")
    secrets = read_secrets(path)
    assert (secrets.wuzapi_base_url, secrets.wuzapi_session_token) == ("http://w:8080", "tok")
    assert "tok" not in repr(secrets)
