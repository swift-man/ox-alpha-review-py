import asyncio
import hashlib
import hmac
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ox_alpha_review.config import Settings
from ox_alpha_review.infrastructure.sqlite_state import (
    SQLiteCompletionAttemptJournal,
    SQLiteProductionReadiness,
    SQLiteSafetyDatabase,
)
from ox_alpha_review.main import create_app


def _settings(tmp_path: Path, *, dry_run: bool = True) -> Settings:
    key_file = tmp_path / "github-app.pem"
    key_file.write_text("test-only-key", encoding="utf-8")
    key_file.chmod(0o600)
    return Settings(
        _env_file=None,
        GITHUB_APP_ID=1,
        GITHUB_APP_PRIVATE_KEY_PATH=key_file,
        GITHUB_WEBHOOK_SECRET="webhook-secret",
        GITHUB_APP_SLUG="ox-alpha-review-bot",
        OPENROUTER_API_KEY="test-inference-key",
        REPO_CACHE_DIR=tmp_path / "repos",
        DRY_RUN=dry_run,
        REVIEW_CONCURRENCY=1,
    )


def _signature(body: bytes) -> str:
    digest = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_health_ping_and_readiness_without_external_calls(tmp_path: Path) -> None:
    app = create_app(
        _settings(tmp_path),
        state_database=tmp_path / "state" / "safety.sqlite3",
    )
    body = b"{}"
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").status_code == 503
        ping = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": _signature(body),
                "X-GitHub-Event": "ping",
                "X-GitHub-Delivery": "ping-delivery",
            },
        )
        assert (ping.status_code, ping.text) == (200, "pong")


def test_unsigned_body_is_rejected_before_json_parsing(tmp_path: Path) -> None:
    app = create_app(
        _settings(tmp_path),
        state_database=tmp_path / "state" / "safety.sqlite3",
    )
    with TestClient(app) as client:
        response = client.post(
            "/webhook",
            content=b"not-json",
            headers={"X-GitHub-Event": "ping"},
        )

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"not-json", "invalid json"),
        (b"\xff", "invalid utf-8"),
        (b"[]", "invalid payload"),
    ],
)
def test_signed_invalid_payload_is_rejected(
    tmp_path: Path,
    body: bytes,
    message: str,
) -> None:
    app = create_app(
        _settings(tmp_path),
        state_database=tmp_path / "state" / "safety.sqlite3",
    )
    with TestClient(app) as client:
        response = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": _signature(body),
                "X-GitHub-Event": "ping",
                "X-GitHub-Delivery": "invalid-payload",
            },
        )

    assert (response.status_code, response.text) == (400, message)


def test_pull_request_is_refused_until_written_acceptance(tmp_path: Path) -> None:
    app = create_app(
        _settings(tmp_path),
        state_database=tmp_path / "state" / "safety.sqlite3",
    )
    body = b"{}"
    with TestClient(app) as client:
        response = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": _signature(body),
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "delivery",
            },
        )

    assert response.status_code == 503
    assert "confirmation" in response.text


def test_dry_run_blocks_inference_even_after_written_acceptance(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "safety.sqlite3"
    database = SQLiteSafetyDatabase(state_path)

    async def accept() -> None:
        await database.initialize()
        await SQLiteProductionReadiness(database).record(
            key_fingerprint=hashlib.sha256(b"test-inference-key").hexdigest(),
            confirmation_sha256="a" * 64,
            accepted_at=datetime(2026, 8, 24, tzinfo=UTC),
        )

    asyncio.run(accept())
    app = create_app(_settings(tmp_path), state_database=state_path)

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert "DRY_RUN disables OpenRouter inference" in response.text


def test_unverified_attempt_blocks_readiness_and_webhook_after_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "safety.sqlite3"
    database = SQLiteSafetyDatabase(state_path)

    async def prepare_crash_state() -> None:
        await database.initialize()
        await SQLiteProductionReadiness(database).record(
            key_fingerprint=hashlib.sha256(b"test-inference-key").hexdigest(),
            confirmation_sha256="a" * 64,
            accepted_at=datetime(2026, 8, 24, tzinfo=UTC),
        )
        await SQLiteCompletionAttemptJournal(database).begin(
            "sent-before-crash",
            datetime(2026, 8, 24, tzinfo=UTC),
        )

    asyncio.run(prepare_crash_state())
    app = create_app(_settings(tmp_path, dry_run=False), state_database=state_path)
    body = b"{}"

    with TestClient(app) as client:
        readiness_response = client.get("/readyz")
        webhook_response = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": _signature(body),
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "after-crash",
            },
        )
        queue_size = app.state.handler._queue.qsize()  # noqa: SLF001

    assert readiness_response.status_code == 503
    assert "unverified completion attempt" in readiness_response.text
    assert webhook_response.status_code == 503
    assert queue_size == 0
