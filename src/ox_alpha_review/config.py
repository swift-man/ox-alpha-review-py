from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, StringConstraints, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

_ALLOWED_OPENROUTER_ENV = {"OPENROUTER_API_KEY"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    github_app_id: int = Field(..., gt=0, alias="GITHUB_APP_ID")
    github_app_private_key_path: Path | None = Field(
        default=None,
        alias="GITHUB_APP_PRIVATE_KEY_PATH",
    )
    github_app_private_key: str | None = Field(
        default=None,
        alias="GITHUB_APP_PRIVATE_KEY",
    )
    github_webhook_secret: NonBlankStr = Field(..., alias="GITHUB_WEBHOOK_SECRET")
    github_app_slug: NonBlankStr = Field(..., alias="GITHUB_APP_SLUG")
    github_api_base: NonBlankStr = Field(
        default="https://api.github.com",
        alias="GITHUB_API_BASE",
    )
    openrouter_api_key: NonBlankStr = Field(..., alias="OPENROUTER_API_KEY")

    host: NonBlankStr = Field(default="127.0.0.1", alias="HOST")
    port: int = Field(default=8025, ge=1, le=65535, alias="PORT")
    dry_run: bool = Field(default=True, alias="DRY_RUN")
    repo_cache_dir: Path = Field(
        default=Path.home() / ".ox-alpha-review-py" / "repos",
        alias="REPO_CACHE_DIR",
    )
    git_timeout_sec: int = Field(default=120, gt=0, le=600, alias="GIT_TIMEOUT_SEC")
    file_max_bytes: int = Field(
        default=204_800,
        gt=0,
        le=2 * 1024 * 1024,
        alias="FILE_MAX_BYTES",
    )
    data_file_max_bytes: int = Field(
        default=20_000,
        gt=0,
        le=200_000,
        alias="DATA_FILE_MAX_BYTES",
    )
    webhook_max_body_bytes: int = Field(
        default=2 * 1024 * 1024,
        gt=0,
        le=10 * 1024 * 1024,
        alias="WEBHOOK_MAX_BODY_BYTES",
    )
    review_concurrency: int = Field(
        default=1,
        ge=1,
        le=1,
        alias="REVIEW_CONCURRENCY",
    )
    review_queue_maxsize: int = Field(
        default=10,
        gt=0,
        le=100,
        alias="REVIEW_QUEUE_MAXSIZE",
    )

    @model_validator(mode="after")
    def validate_private_key_source(self) -> Settings:
        configured = (
            self.github_app_private_key_path is not None,
            self.github_app_private_key is not None,
        )
        if sum(configured) != 1:
            raise ValueError(
                "exactly one of GITHUB_APP_PRIVATE_KEY_PATH or GITHUB_APP_PRIVATE_KEY is required"
            )
        if (
            self.github_app_private_key_path is not None
            and not self.github_app_private_key_path.is_absolute()
        ):
            raise ValueError("GITHUB_APP_PRIVATE_KEY_PATH must be absolute")
        if not self.repo_cache_dir.is_absolute():
            raise ValueError("REPO_CACHE_DIR must be absolute")
        github_url = urlsplit(self.github_api_base)
        if github_url.scheme != "https" or not github_url.netloc:
            raise ValueError("GITHUB_API_BASE must be an absolute HTTPS URL")
        return self

    @property
    def state_dir(self) -> Path:
        return Path.home() / ".ox-alpha-review-py"

    @property
    def state_database(self) -> Path:
        return self.state_dir / "safety.sqlite3"

    @property
    def max_input_tokens(self) -> int:
        return 180_000

    @property
    def max_output_tokens(self) -> int:
        return 16_384

    def load_private_key(self) -> str:
        if self.github_app_private_key is not None:
            return self.github_app_private_key.replace("\\n", "\n")
        path = self.github_app_private_key_path
        if path is None:
            raise RuntimeError("GitHub App private key source is missing")
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError as exc:
            raise RuntimeError("GitHub App private key file is unavailable") from exc
        if mode != 0o600:
            raise RuntimeError("GitHub App private key file mode must be 0600")
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError("GitHub App private key file is unreadable") from exc


def load_settings() -> Settings:
    env_file = Path(".env")
    if env_file.exists() and stat.S_IMODE(env_file.stat().st_mode) != 0o600:
        raise RuntimeError(".env file mode must be 0600")
    forbidden = sorted(
        name
        for name in os.environ
        if name.startswith("OPENROUTER_") and name not in _ALLOWED_OPENROUTER_ENV
    )
    if forbidden:
        names = ", ".join(forbidden)
        raise RuntimeError(f"forbidden OpenRouter runtime environment variables: {names}")
    return Settings()  # type: ignore[call-arg]
