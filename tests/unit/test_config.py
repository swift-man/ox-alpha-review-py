from pathlib import Path

import pytest
from pydantic import ValidationError

from ox_alpha_review.config import Settings, load_settings


def _values(tmp_path: Path) -> dict[str, object]:
    key_file = tmp_path / "app.pem"
    key_file.write_text("pem", encoding="utf-8")
    key_file.chmod(0o600)
    return {
        "_env_file": None,
        "GITHUB_APP_ID": 1,
        "GITHUB_APP_PRIVATE_KEY_PATH": key_file,
        "GITHUB_WEBHOOK_SECRET": "secret",
        "GITHUB_APP_SLUG": "ox-alpha-review-bot",
        "OPENROUTER_API_KEY": "inference-key",
    }


def test_settings_require_exactly_one_private_key_source(tmp_path: Path) -> None:
    values = _values(tmp_path)
    values["GITHUB_APP_PRIVATE_KEY"] = "inline"

    with pytest.raises(ValidationError, match="exactly one"):
        Settings(**values)  # type: ignore[arg-type]


def test_settings_require_serial_review_concurrency(tmp_path: Path) -> None:
    values = _values(tmp_path)
    values["REVIEW_CONCURRENCY"] = 2

    with pytest.raises(ValidationError):
        Settings(**values)  # type: ignore[arg-type]


def test_private_key_file_must_be_mode_0600(tmp_path: Path) -> None:
    values = _values(tmp_path)
    key_file = values["GITHUB_APP_PRIVATE_KEY_PATH"]
    assert isinstance(key_file, Path)
    key_file.chmod(0o644)

    with pytest.raises(RuntimeError, match="0600"):
        Settings(**values).load_private_key()  # type: ignore[arg-type]


def test_reserved_openrouter_override_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_MODEL", "paid/model")

    with pytest.raises(RuntimeError, match="OPENROUTER_MODEL"):
        load_settings()


def test_env_file_must_be_mode_0600(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=test\n", encoding="utf-8")
    env_file.chmod(0o644)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError, match="0600"):
        load_settings()


def test_github_api_base_must_use_https(tmp_path: Path) -> None:
    values = _values(tmp_path)
    values["GITHUB_API_BASE"] = "http://api.github.test"

    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(**values)  # type: ignore[arg-type]
