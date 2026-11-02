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
