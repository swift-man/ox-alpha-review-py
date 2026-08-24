from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response

from ox_alpha_review.application.guarded_completion import GuardedCompletion
from ox_alpha_review.application.ports import StateSafetyError
from ox_alpha_review.config import Settings, load_settings
from ox_alpha_review.domain import FreeOnlyPolicy
from ox_alpha_review.infrastructure.openrouter import (
    OPENROUTER_ORIGIN,
    OpenRouterCompletionTransport,
    OpenRouterKeyMetadataReader,
    OpenRouterModelCatalogReader,
)
from ox_alpha_review.infrastructure.ox_alpha_engine import OxAlphaReviewEngine
from ox_alpha_review.infrastructure.ox_alpha_prompt import build_prompt
from ox_alpha_review.infrastructure.process_lock import ExclusiveProcessLock
from ox_alpha_review.infrastructure.runtime_primitives import SystemClock, UuidFactory
from ox_alpha_review.infrastructure.sqlite_state import (
    SQLiteCompletionAttemptJournal,
    SQLiteDeliveryStore,
    SQLiteFreeQuotaLedger,
    SQLiteProductionReadiness,
    SQLiteSafetyDatabase,
    SQLiteSafetyLatch,
)
from reviewbot_common.application.follow_up_use_case import (
    FollowUpReviewUseCase,
    normalize_bot_user_login,
)
from reviewbot_common.application.review_pr_use_case import ReviewPullRequestUseCase
from reviewbot_common.application.webhook_handler import WebhookHandler
from reviewbot_common.infrastructure.diff_context_collector import DiffContextCollector
from reviewbot_common.infrastructure.file_dump_collector import FileDumpCollector
from reviewbot_common.infrastructure.git_repo_fetcher import GitRepoFetcher
from reviewbot_common.infrastructure.github_app_client import (
    GitHubAppClient,
    _default_tls_context,
)
from reviewbot_common.logging_utils import configure_logging

logger = logging.getLogger(__name__)


class RequestBodyTooLarge(RuntimeError):
    pass


async def _read_limited_body(request: Request, max_bytes: int) -> bytes:
    content_length = request.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = -1
        if declared_length > max_bytes:
            raise RequestBodyTooLarge

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise RequestBodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(
    settings: Settings | None = None,
    *,
    state_database: Path | None = None,
) -> FastAPI:
    configure_logging()
    configured = settings or load_settings()
    key_fingerprint = hashlib.sha256(configured.openrouter_api_key.encode("utf-8")).hexdigest()
    database = SQLiteSafetyDatabase(state_database or configured.state_database)
    process_lock = ExclusiveProcessLock(database.path.with_name(f"{database.path.name}.lock"))
    readiness = SQLiteProductionReadiness(database)
    latch = SQLiteSafetyLatch(database)
    attempts = SQLiteCompletionAttemptJournal(database)
    clock = SystemClock()

    async def operational_readiness() -> tuple[bool, str]:
        handler = getattr(app.state, "handler", None)
        if handler is not None:
            healthy, reason = handler.health_status()
            if not healthy:
                return False, reason
        ready, reason = await readiness.status(key_fingerprint)
        if not ready:
            return ready, reason
        if configured.dry_run:
            return False, "DRY_RUN disables OpenRouter inference and GitHub writes"
        try:
            await latch.assert_clear()
            await attempts.assert_clean()
        except StateSafetyError as exc:
            return False, str(exc)
        return True, "ready"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await process_lock.acquire()
        try:
            await database.initialize()
            tls_context = _default_tls_context()
            async with (
                httpx.AsyncClient(
                    base_url=configured.github_api_base,
                    timeout=30.0,
                    verify=tls_context,
                    trust_env=False,
                ) as github_http,
                httpx.AsyncClient(
                    base_url=OPENROUTER_ORIGIN,
                    headers={
                        "Authorization": f"Bearer {configured.openrouter_api_key}",
                        "Content-Type": "application/json",
                        "X-OpenRouter-Title": "ox-alpha-review-py",
                        "X-OpenRouter-Metadata": "enabled",
                    },
                    timeout=httpx.Timeout(600.0, connect=30.0),
                    verify=tls_context,
                    trust_env=False,
                ) as openrouter_http,
            ):
                github = GitHubAppClient(
                    app_id=configured.github_app_id,
                    private_key_pem=configured.load_private_key(),
                    http_client=github_http,
                    dry_run=configured.dry_run,
                    review_model_label="Ox Alpha",
                )
                policy = FreeOnlyPolicy()
                completion = GuardedCompletion(
                    key_reader=OpenRouterKeyMetadataReader(openrouter_http),
                    catalog_reader=OpenRouterModelCatalogReader(openrouter_http),
                    quota=SQLiteFreeQuotaLedger(database),
                    attempts=attempts,
                    latch=latch,
                    readiness=readiness,
                    transport=OpenRouterCompletionTransport(openrouter_http, policy),
                    policy=policy,
                    clock=clock,
                    identifiers=UuidFactory(),
                    key_fingerprint=key_fingerprint,
                )
                engine = OxAlphaReviewEngine(
                    completion,
                    max_output_tokens=configured.max_output_tokens,
                )
                repo_fetcher = GitRepoFetcher(
                    cache_dir=configured.repo_cache_dir,
                    git_timeout_sec=configured.git_timeout_sec,
                )
                collector = FileDumpCollector(
                    file_max_bytes=configured.file_max_bytes,
                    data_file_max_bytes=configured.data_file_max_bytes,
                    git_timeout_sec=configured.git_timeout_sec,
                )
                diff_collector = DiffContextCollector(
                    overhead_estimator=lambda pr, dump, history: len(
                        build_prompt(pr, dump, history=history).encode("utf-8")
                    )
                )
                bot_login = normalize_bot_user_login(configured.github_app_slug)
                use_case = ReviewPullRequestUseCase(
                    github=github,
                    repo_fetcher=repo_fetcher,
                    file_collector=collector,
                    engine=engine,
                    max_input_tokens=configured.max_input_tokens,
                    diff_context_collector=diff_collector,
                    bot_login=bot_login,
                    engine_label="Ox Alpha Review",
                    max_input_tokens_env="hard-coded free-only input limit",
                    model_env="hard-coded stealth/ox-alpha",
                    enable_diff_fallback_env="hard-coded diff fallback",
                )
                follow_up = FollowUpReviewUseCase(
                    github=github,
                    repo_fetcher=repo_fetcher,
                    bot_user_login=bot_login,
                )
                handler = WebhookHandler(
                    secret=configured.github_webhook_secret,
                    github=github,
                    use_case=use_case,
                    concurrency=configured.review_concurrency,
                    queue_maxsize=configured.review_queue_maxsize,
                    follow_up_use_case=follow_up,
                    delivery_store=SQLiteDeliveryStore(database, clock),
                )
                app.state.handler = handler
                await handler.start()
                try:
                    yield
                finally:
                    await handler.stop()
        finally:
            await process_lock.release()

    app = FastAPI(title="ox-alpha-review", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Response:
        ready, reason = await operational_readiness()
        if ready:
            return Response(status_code=200, content="ready")
        return Response(status_code=503, content=reason)

    @app.post("/webhook")
    async def webhook(request: Request) -> Response:
        handler: WebhookHandler = request.app.state.handler
        try:
            body = await _read_limited_body(request, configured.webhook_max_body_bytes)
        except RequestBodyTooLarge:
            logger.warning("webhook body too large")
            return Response(status_code=413, content="payload too large")

        signature = request.headers.get("X-Hub-Signature-256")
        if not handler.verify_signature(signature, body):
            logger.warning("invalid webhook signature")
            return Response(status_code=401, content="invalid signature")
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except UnicodeDecodeError:
            return Response(status_code=400, content="invalid utf-8")
        except json.JSONDecodeError:
            return Response(status_code=400, content="invalid json")
        if not isinstance(payload, dict):
            return Response(status_code=400, content="invalid payload")

        event = request.headers.get("X-GitHub-Event", "")
        delivery = request.headers.get("X-GitHub-Delivery", "")
        if event == "pull_request":
            ready, reason = await operational_readiness()
            if not ready:
                return Response(status_code=503, content=reason)
        status, result = await handler.accept(event, delivery, payload)
        return Response(status_code=status, content=result)

    return app


def app_factory() -> FastAPI:
    return create_app()
