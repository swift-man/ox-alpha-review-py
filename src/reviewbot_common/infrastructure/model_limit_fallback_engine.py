import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from reviewbot_common.domain import FileDump, PullRequest, ReviewHistory, ReviewResult
from reviewbot_common.domain.model_limit import (
    extract_model_limit_reset_hint,
    is_global_model_limit_error,
    is_model_limit_error,
)
from reviewbot_common.interfaces import ModelLimitDetail, ReviewEngine, ReviewEngineError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewEngineAttempt:
    label: str
    engine: ReviewEngine


class ModelLimitFallbackReviewEngine:
    """Retry with a secondary engine only when the primary engine hits a model limit."""

    def __init__(
        self,
        primary: ReviewEngine,
        fallback: ReviewEngine,
        *,
        fallback_label: str,
        primary_label: str = "primary",
        primary_limit_ttl_sec: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._primary_label = primary_label
        self._fallback_label = fallback_label
        self._primary_limit_ttl_sec = primary_limit_ttl_sec
        self._clock = clock
        self._global_primary_limit: _CachedModelLimit | None = None

    async def review(
        self,
        pr: PullRequest,
        dump: FileDump,
        *,
        history: ReviewHistory | None = None,
    ) -> ReviewResult:
        active_primary_limit = self._active_global_primary_limit()
        if active_primary_limit is not None:
            logger.warning(
                "primary review engine recently hit a global model limit; "
                "skipping primary for %s#%d and using %s",
                pr.repo.full_name,
                pr.number,
                self._fallback_label,
            )
            return await self._review_with_fallback_label(
                pr,
                dump,
                history=history,
                primary_limit=active_primary_limit.detail,
            )

        try:
            return await self._primary.review(pr, dump, history=history)
        except ReviewEngineError as exc:
            if not is_model_limit_error(exc):
                raise
            primary_limit = _model_limit_detail(self._primary_label, exc)
            if is_global_model_limit_error(exc):
                self._remember_global_primary_limit(primary_limit)
            logger.warning(
                "primary review engine hit a model limit for %s#%d; retrying with %s",
                pr.repo.full_name,
                pr.number,
                self._fallback_label,
            )

        return await self._review_with_fallback_label(
            pr,
            dump,
            history=history,
            primary_limit=primary_limit,
        )

    async def _review_with_fallback_label(
        self,
        pr: PullRequest,
        dump: FileDump,
        *,
        history: ReviewHistory | None,
        primary_limit: ModelLimitDetail | None = None,
    ) -> ReviewResult:
        try:
            result = await self._fallback.review(pr, dump, history=history)
        except ReviewEngineError as exc:
            raise _combined_fallback_error(
                fallback_label=self._fallback_label,
                fallback_error=exc,
                primary_limit=primary_limit,
            ) from exc
        return replace(result, model_label=self._fallback_label)

    def _remember_global_primary_limit(
        self,
        detail: ModelLimitDetail,
    ) -> None:
        now = self._clock()
        self._global_primary_limit = _CachedModelLimit(
            expires_at=now + self._primary_limit_ttl_sec,
            detail=detail,
        )

    def _active_global_primary_limit(self) -> "_CachedModelLimit | None":
        now = self._clock()
        if self._global_primary_limit is not None and self._global_primary_limit.expires_at <= now:
            self._global_primary_limit = None
        return self._global_primary_limit


class ModelLimitFallbackChainReviewEngine:
    """Try review engines in priority order, advancing only on model-limit failures."""

    def __init__(
        self,
        attempts: Sequence[ReviewEngineAttempt],
        *,
        limit_ttl_sec: float = 300.0,
        rotating_attempt_start: int = 0,
        rotating_attempt_count: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not attempts:
            raise ValueError("at least one review engine attempt is required")
        if rotating_attempt_start < 0:
            raise ValueError("rotating_attempt_start must be >= 0")
        if rotating_attempt_count < 0:
            raise ValueError("rotating_attempt_count must be >= 0")
        if rotating_attempt_start + rotating_attempt_count > len(attempts):
            raise ValueError("rotating attempt range must fit within attempts")
        self._attempts = tuple(attempts)
        self._limit_ttl_sec = limit_ttl_sec
        self._rotating_attempt_start = rotating_attempt_start
        self._rotating_attempt_count = rotating_attempt_count
        self._last_successful_rotating_position: int | None = None
        self._clock = clock
        self._global_limits: dict[int, _CachedModelLimit] = {}

    async def review(
        self,
        pr: PullRequest,
        dump: FileDump,
        *,
        history: ReviewHistory | None = None,
    ) -> ReviewResult:
        limit_details: list[ModelLimitDetail] = []
        message_parts: list[str] = []

        for index, attempt in self._ordered_attempts():
            active_limit = self._active_global_limit(index)
            if active_limit is not None:
                limit_details.append(active_limit.detail)
                message_parts.append(
                    f"{active_limit.detail.model_label}: {active_limit.detail.message}"
                )
                logger.warning(
                    "review engine %s recently hit a global model limit; skipping for %s#%d",
                    attempt.label,
                    pr.repo.full_name,
                    pr.number,
                )
                continue

            try:
                result = await attempt.engine.review(pr, dump, history=history)
            except ReviewEngineError as exc:
                if not is_model_limit_error(exc):
                    if limit_details:
                        raise _combined_chain_non_limit_error(
                            limit_details=tuple(limit_details),
                            message_parts=tuple(message_parts),
                            attempt_label=attempt.label,
                            attempt_error=exc,
                        ) from exc
                    raise

                details = _model_limit_details(attempt.label, exc)
                limit_details.extend(details)
                message_parts.extend(
                    f"{detail.model_label}: {detail.message}" for detail in details
                )
                if is_global_model_limit_error(exc):
                    self._remember_global_limit(index, details[0])
                logger.warning(
                    "review engine %s hit a model limit for %s#%d; trying next engine",
                    attempt.label,
                    pr.repo.full_name,
                    pr.number,
                )
                continue

            self._remember_successful_attempt(index)
            return replace(result, model_label=attempt.label)

        raise _combined_chain_model_limit_error(
            limit_details=tuple(limit_details),
            message_parts=tuple(message_parts),
        )

    def _remember_global_limit(self, index: int, detail: ModelLimitDetail) -> None:
        now = self._clock()
        self._global_limits[index] = _CachedModelLimit(
            expires_at=now + self._limit_ttl_sec,
            detail=detail,
        )

    def _active_global_limit(self, index: int) -> "_CachedModelLimit | None":
        cached = self._global_limits.get(index)
        if cached is None:
            return None
        if cached.expires_at <= self._clock():
            del self._global_limits[index]
            return None
        return cached

    def _ordered_attempts(self) -> tuple[tuple[int, ReviewEngineAttempt], ...]:
        indexed_attempts = tuple(enumerate(self._attempts))
        if self._rotating_attempt_count <= 1 or self._last_successful_rotating_position is None:
            return indexed_attempts

        start = self._rotating_attempt_start
        stop = start + self._rotating_attempt_count
        segment = indexed_attempts[start:stop]
        offset = self._last_successful_rotating_position
        return (
            indexed_attempts[:start] + segment[offset:] + segment[:offset] + indexed_attempts[stop:]
        )

    def _remember_successful_attempt(self, index: int) -> None:
        start = self._rotating_attempt_start
        stop = start + self._rotating_attempt_count
        if not start <= index < stop:
            return
        self._last_successful_rotating_position = index - start


@dataclass(frozen=True)
class _CachedModelLimit:
    expires_at: float
    detail: ModelLimitDetail


def _model_limit_detail(label: str, exc: BaseException) -> ModelLimitDetail:
    message = str(exc)
    return ModelLimitDetail(
        model_label=label,
        message=message,
        reset_hint=extract_model_limit_reset_hint(message),
    )


def _combined_fallback_error(
    *,
    fallback_label: str,
    fallback_error: ReviewEngineError,
    primary_limit: ModelLimitDetail | None,
) -> ReviewEngineError:
    details = []
    if primary_limit is not None:
        details.append(primary_limit)
    fallback_is_model_limit = is_model_limit_error(fallback_error)
    if fallback_is_model_limit:
        details.append(_model_limit_detail(fallback_label, fallback_error))

    message_parts = []
    if primary_limit is not None:
        message_parts.append(f"{primary_limit.model_label}: {primary_limit.message}")
    message_parts.append(f"{fallback_label}: {fallback_error}")
    non_limit_failure_message = None
    if not fallback_is_model_limit:
        fallback_message = fallback_error.non_limit_failure_message or str(fallback_error)
        non_limit_failure_message = f"{fallback_label}: {fallback_message}"
    return ReviewEngineError(
        "model-limit fallback failed; " + " | ".join(message_parts),
        returncode=fallback_error.returncode,
        limit_details=tuple(details),
        non_limit_failure_message=non_limit_failure_message,
    )


def _model_limit_details(label: str, exc: ReviewEngineError) -> tuple[ModelLimitDetail, ...]:
    if exc.limit_details:
        return exc.limit_details
    return (_model_limit_detail(label, exc),)


def _combined_chain_model_limit_error(
    *,
    limit_details: tuple[ModelLimitDetail, ...],
    message_parts: tuple[str, ...],
) -> ReviewEngineError:
    message = "model-limit fallback chain failed"
    if message_parts:
        message += "; " + " | ".join(message_parts)
    return ReviewEngineError(message, limit_details=limit_details)


def _combined_chain_non_limit_error(
    *,
    limit_details: tuple[ModelLimitDetail, ...],
    message_parts: tuple[str, ...],
    attempt_label: str,
    attempt_error: ReviewEngineError,
) -> ReviewEngineError:
    detail = attempt_error.non_limit_failure_message or str(attempt_error)
    non_limit_failure_message = f"{attempt_label}: {detail}"
    message = "model-limit fallback chain failed; "
    message += " | ".join((*message_parts, non_limit_failure_message))
    return ReviewEngineError(
        message,
        returncode=attempt_error.returncode,
        limit_details=limit_details,
        non_limit_failure_message=non_limit_failure_message,
    )
