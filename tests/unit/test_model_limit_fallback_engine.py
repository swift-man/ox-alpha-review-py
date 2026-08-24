import pytest

from reviewbot_common.domain import (
    FileDump,
    FileEntry,
    PullRequest,
    RepoRef,
    ReviewEvent,
    ReviewHistory,
    ReviewResult,
)
from reviewbot_common.infrastructure.model_limit_fallback_engine import (
    ModelLimitFallbackChainReviewEngine,
    ModelLimitFallbackReviewEngine,
    ReviewEngineAttempt,
)
from reviewbot_common.interfaces import ReviewEngineError


class _Engine:
    def __init__(
        self,
        *,
        result: ReviewResult | None = None,
        error: ReviewEngineError | None = None,
    ) -> None:
        self.calls = 0
        self._result = result
        self._error = error

    async def review(
        self,
        pr: PullRequest,
        dump: FileDump,
        *,
        history: ReviewHistory | None = None,
    ) -> ReviewResult:
        _ = (pr, dump, history)
        self.calls += 1
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class _SequenceEngine:
    def __init__(self, outcomes: list[ReviewResult | ReviewEngineError]) -> None:
        self.calls = 0
        self._outcomes = outcomes

    async def review(
        self,
        pr: PullRequest,
        dump: FileDump,
        *,
        history: ReviewHistory | None = None,
    ) -> ReviewResult:
        _ = (pr, dump, history)
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, ReviewEngineError):
            raise outcome
        return outcome


def _pr(*, number: int = 7, head_sha: str = "abc123") -> PullRequest:
    return PullRequest(
        repo=RepoRef(owner="owner", name="repo"),
        number=number,
        title="Test PR",
        body="",
        head_sha=head_sha,
        head_ref="feature",
        base_sha="def456",
        base_ref="main",
        clone_url="https://github.com/owner/repo.git",
        changed_files=("src/app.py",),
        installation_id=42,
        is_draft=False,
    )


def _dump() -> FileDump:
    return FileDump(
        entries=(FileEntry("src/app.py", "print('hello')\n", 15, True),),
        total_chars=15,
    )


@pytest.mark.asyncio
async def test_model_limit_error_uses_fallback_engine() -> None:
    primary = _Engine(error=ReviewEngineError("You've hit your weekly limit"))
    fallback = _Engine(result=ReviewResult(summary="ok", event=ReviewEvent.APPROVE))
    engine = ModelLimitFallbackReviewEngine(
        primary,
        fallback,
        fallback_label="Claude Opus 4.6 (Thinking)",
    )

    result = await engine.review(_pr(), _dump())

    assert primary.calls == 1
    assert fallback.calls == 1
    assert result.summary == "ok"
    assert result.model_label == "Claude Opus 4.6 (Thinking)"


@pytest.mark.asyncio
async def test_non_limit_error_does_not_use_fallback_engine() -> None:
    primary = _Engine(error=ReviewEngineError("invalid model name"))
    fallback = _Engine(result=ReviewResult(summary="ok", event=ReviewEvent.APPROVE))
    engine = ModelLimitFallbackReviewEngine(
        primary,
        fallback,
        fallback_label="Claude Opus 4.6 (Thinking)",
    )

    with pytest.raises(ReviewEngineError, match="invalid model name"):
        await engine.review(_pr(), _dump())

    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_fallback_error_preserves_existing_non_limit_failure_message() -> None:
    primary = _Engine(error=ReviewEngineError("You've hit your weekly limit"))
    fallback = _Engine(
        error=ReviewEngineError(
            "model-limit fallback failed",
            non_limit_failure_message="nested fallback: timed out after 600s",
        )
    )
    engine = ModelLimitFallbackReviewEngine(
        primary,
        fallback,
        fallback_label="Claude Opus 4.6 (Thinking)",
    )

    with pytest.raises(ReviewEngineError) as captured:
        await engine.review(_pr(), _dump())

    assert (
        captured.value.non_limit_failure_message
        == "Claude Opus 4.6 (Thinking): nested fallback: timed out after 600s"
    )


@pytest.mark.asyncio
async def test_recent_primary_limit_skips_primary_on_next_attempt() -> None:
    primary = _Engine(error=ReviewEngineError("You've hit your session limit"))
    fallback = _SequenceEngine(
        [
            ReviewEngineError("agy full review timed out"),
            ReviewResult(summary="diff ok", event=ReviewEvent.APPROVE),
        ]
    )
    engine = ModelLimitFallbackReviewEngine(
        primary,
        fallback,
        fallback_label="Claude Opus 4.6 (Thinking)",
    )
    pr = _pr()

    with pytest.raises(ReviewEngineError, match="agy full review timed out"):
        await engine.review(pr, _dump())
    result = await engine.review(pr, _dump())

    assert primary.calls == 1
    assert fallback.calls == 2
    assert result.summary == "diff ok"


@pytest.mark.asyncio
async def test_global_quota_limit_skips_primary_for_other_pr() -> None:
    primary = _Engine(
        error=ReviewEngineError("RESOURCE_EXHAUSTED: quota reached. Resets in 50h46m4s")
    )
    fallback = _SequenceEngine(
        [
            ReviewEngineError("agy full review timed out"),
            ReviewResult(summary="other pr ok", event=ReviewEvent.APPROVE),
        ]
    )
    engine = ModelLimitFallbackReviewEngine(
        primary,
        fallback,
        fallback_label="Claude Opus 4.6 (Thinking)",
    )

    with pytest.raises(ReviewEngineError, match="agy full review timed out"):
        await engine.review(_pr(number=7, head_sha="abc123"), _dump())
    result = await engine.review(_pr(number=8, head_sha="def456"), _dump())

    assert primary.calls == 1
    assert fallback.calls == 2
    assert result.summary == "other pr ok"


@pytest.mark.asyncio
async def test_context_limit_does_not_skip_primary_for_other_pr() -> None:
    primary = _SequenceEngine(
        [
            ReviewEngineError("maximum context window exceeded"),
            ReviewResult(summary="primary ok", event=ReviewEvent.APPROVE),
        ]
    )
    fallback = _Engine(result=ReviewResult(summary="fallback ok", event=ReviewEvent.APPROVE))
    engine = ModelLimitFallbackReviewEngine(
        primary,
        fallback,
        fallback_label="Claude Opus 4.6 (Thinking)",
    )

    first = await engine.review(_pr(number=7, head_sha="abc123"), _dump())
    second = await engine.review(_pr(number=8, head_sha="def456"), _dump())

    assert primary.calls == 2
    assert fallback.calls == 1
    assert first.summary == "fallback ok"
    assert second.summary == "primary ok"


@pytest.mark.asyncio
async def test_context_limit_retries_primary_for_same_pr() -> None:
    primary = _SequenceEngine(
        [
            ReviewEngineError("maximum context window exceeded"),
            ReviewResult(summary="diff primary ok", event=ReviewEvent.APPROVE),
        ]
    )
    fallback = _Engine(error=ReviewEngineError("agy full review timed out"))
    engine = ModelLimitFallbackReviewEngine(
        primary,
        fallback,
        fallback_label="Claude Opus 4.6 (Thinking)",
    )
    pr = _pr()

    with pytest.raises(ReviewEngineError, match="agy full review timed out"):
        await engine.review(pr, _dump())
    result = await engine.review(pr, _dump())

    assert primary.calls == 2
    assert fallback.calls == 1
    assert result.summary == "diff primary ok"


@pytest.mark.asyncio
async def test_expired_global_primary_limit_is_cleared() -> None:
    now = 100.0

    def clock() -> float:
        return now

    primary = _Engine(error=ReviewEngineError("You've hit your weekly limit"))
    fallback = _Engine(result=ReviewResult(summary="fallback ok", event=ReviewEvent.APPROVE))
    engine = ModelLimitFallbackReviewEngine(
        primary,
        fallback,
        fallback_label="Claude Opus 4.6 (Thinking)",
        primary_limit_ttl_sec=5,
        clock=clock,
    )

    await engine.review(_pr(), _dump())
    assert engine._global_primary_limit is not None

    now = 106.0
    assert engine._active_global_primary_limit() is None

    assert engine._global_primary_limit is None


@pytest.mark.asyncio
async def test_global_primary_limit_expiry_does_not_block_later_primary() -> None:
    now = 100.0

    def clock() -> float:
        return now

    primary = _SequenceEngine(
        [
            ReviewEngineError("You've hit your weekly limit"),
            ReviewResult(summary="primary ok", event=ReviewEvent.APPROVE),
        ]
    )
    fallback = _Engine(result=ReviewResult(summary="fallback ok", event=ReviewEvent.APPROVE))
    engine = ModelLimitFallbackReviewEngine(
        primary,
        fallback,
        fallback_label="Claude Opus 4.6 (Thinking)",
        primary_limit_ttl_sec=5,
        clock=clock,
    )

    await engine.review(_pr(number=7, head_sha="abc123"), _dump())
    assert primary.calls == 1
    assert fallback.calls == 1

    now = 106.0
    result = await engine.review(_pr(number=8, head_sha="def456"), _dump())

    assert primary.calls == 2
    assert fallback.calls == 1
    assert result.summary == "primary ok"


@pytest.mark.asyncio
async def test_combined_limit_error_preserves_primary_and_fallback_reset_times() -> None:
    primary = _Engine(
        error=ReviewEngineError(
            "claude -p failed (rc=1, model=claude-opus-4-8): "
            "You've hit your session limit · resets 11:30pm (Asia/Seoul)"
        )
    )
    fallback = _Engine(
        error=ReviewEngineError(
            "agy --print failed (rc=1, model=Claude Opus 4.6 (Thinking)): "
            "RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 50h46m4s."
        )
    )
    engine = ModelLimitFallbackReviewEngine(
        primary,
        fallback,
        fallback_label="Claude Opus 4.6 (Thinking)",
        primary_label="claude-opus-4-8",
    )

    with pytest.raises(ReviewEngineError) as raised:
        await engine.review(_pr(), _dump())

    assert "claude-opus-4-8" in str(raised.value)
    assert "Claude Opus 4.6 (Thinking)" in str(raised.value)
    assert [d.model_label for d in raised.value.limit_details] == [
        "claude-opus-4-8",
        "Claude Opus 4.6 (Thinking)",
    ]
    assert [d.reset_hint for d in raised.value.limit_details] == [
        "11:30pm (Asia/Seoul)",
        "50h46m4s",
    ]
    assert raised.value.non_limit_failure_message is None


@pytest.mark.asyncio
async def test_combined_error_preserves_non_limit_fallback_failure_message() -> None:
    primary = _Engine(
        error=ReviewEngineError(
            "claude -p failed (rc=1, model=claude-opus-4-8): "
            "You've hit your session limit · resets 11:30pm (Asia/Seoul)"
        )
    )
    fallback = _Engine(error=ReviewEngineError("agy --print timed out after 600s"))
    engine = ModelLimitFallbackReviewEngine(
        primary,
        fallback,
        fallback_label="Claude Opus 4.6 (Thinking)",
        primary_label="claude-opus-4-8",
    )

    with pytest.raises(ReviewEngineError) as raised:
        await engine.review(_pr(), _dump())

    assert [d.model_label for d in raised.value.limit_details] == ["claude-opus-4-8"]
    assert [d.reset_hint for d in raised.value.limit_details] == ["11:30pm (Asia/Seoul)"]
    assert raised.value.non_limit_failure_message == (
        "Claude Opus 4.6 (Thinking): agy --print timed out after 600s"
    )


@pytest.mark.asyncio
async def test_chain_uses_first_available_46_thinking_account() -> None:
    first = _Engine(result=ReviewResult(summary="first ok", event=ReviewEvent.APPROVE))
    second = _Engine(result=ReviewResult(summary="second ok", event=ReviewEvent.APPROVE))
    claude = _Engine(result=ReviewResult(summary="claude ok", event=ReviewEvent.APPROVE))
    engine = ModelLimitFallbackChainReviewEngine(
        (
            ReviewEngineAttempt("Claude Opus 4.6 (Thinking) #1", first),
            ReviewEngineAttempt("Claude Opus 4.6 (Thinking) #2", second),
            ReviewEngineAttempt("claude-opus-4-8", claude),
        )
    )

    result = await engine.review(_pr(), _dump())

    assert result.summary == "first ok"
    assert result.model_label == "Claude Opus 4.6 (Thinking) #1"
    assert first.calls == 1
    assert second.calls == 0
    assert claude.calls == 0


@pytest.mark.asyncio
async def test_chain_falls_back_to_next_46_account_on_model_limit() -> None:
    first = _Engine(error=ReviewEngineError("RESOURCE_EXHAUSTED. Resets in 1h."))
    second = _Engine(result=ReviewResult(summary="second ok", event=ReviewEvent.APPROVE))
    claude = _Engine(result=ReviewResult(summary="claude ok", event=ReviewEvent.APPROVE))
    engine = ModelLimitFallbackChainReviewEngine(
        (
            ReviewEngineAttempt("Claude Opus 4.6 (Thinking) #1", first),
            ReviewEngineAttempt("Claude Opus 4.6 (Thinking) #2", second),
            ReviewEngineAttempt("claude-opus-4-8", claude),
        )
    )

    result = await engine.review(_pr(), _dump())

    assert result.summary == "second ok"
    assert result.model_label == "Claude Opus 4.6 (Thinking) #2"
    assert first.calls == 1
    assert second.calls == 1
    assert claude.calls == 0


@pytest.mark.asyncio
async def test_chain_uses_claude_after_all_46_accounts_hit_limits() -> None:
    first = _Engine(error=ReviewEngineError("RESOURCE_EXHAUSTED. Resets in 1h."))
    second = _Engine(error=ReviewEngineError("RESOURCE_EXHAUSTED. Resets in 2h."))
    claude = _Engine(result=ReviewResult(summary="claude ok", event=ReviewEvent.APPROVE))
    engine = ModelLimitFallbackChainReviewEngine(
        (
            ReviewEngineAttempt("Claude Opus 4.6 (Thinking) #1", first),
            ReviewEngineAttempt("Claude Opus 4.6 (Thinking) #2", second),
            ReviewEngineAttempt("claude-opus-4-8", claude),
        )
    )

    result = await engine.review(_pr(), _dump())

    assert result.summary == "claude ok"
    assert result.model_label == "claude-opus-4-8"
    assert first.calls == 1
    assert second.calls == 1
    assert claude.calls == 1


@pytest.mark.asyncio
async def test_chain_preserves_all_limit_reset_times_when_exhausted() -> None:
    first = _Engine(error=ReviewEngineError("RESOURCE_EXHAUSTED. Resets in 1h."))
    second = _Engine(error=ReviewEngineError("RESOURCE_EXHAUSTED. Resets in 2h."))
    claude = _Engine(
        error=ReviewEngineError("You've hit your session limit · resets 3:30am (Asia/Seoul)")
    )
    engine = ModelLimitFallbackChainReviewEngine(
        (
            ReviewEngineAttempt("Claude Opus 4.6 (Thinking) #1", first),
            ReviewEngineAttempt("Claude Opus 4.6 (Thinking) #2", second),
            ReviewEngineAttempt("claude-opus-4-8", claude),
        )
    )

    with pytest.raises(ReviewEngineError) as raised:
        await engine.review(_pr(), _dump())

    assert [detail.model_label for detail in raised.value.limit_details] == [
        "Claude Opus 4.6 (Thinking) #1",
        "Claude Opus 4.6 (Thinking) #2",
        "claude-opus-4-8",
    ]
    assert [detail.reset_hint for detail in raised.value.limit_details] == [
        "1h",
        "2h",
        "3:30am (Asia/Seoul)",
    ]
    assert raised.value.non_limit_failure_message is None


@pytest.mark.asyncio
async def test_chain_preserves_non_limit_failure_after_prior_limits() -> None:
    first = _Engine(error=ReviewEngineError("RESOURCE_EXHAUSTED. Resets in 1h."))
    second = _Engine(error=ReviewEngineError("agy --print timed out after 600s"))
    claude = _Engine(result=ReviewResult(summary="claude ok", event=ReviewEvent.APPROVE))
    engine = ModelLimitFallbackChainReviewEngine(
        (
            ReviewEngineAttempt("Claude Opus 4.6 (Thinking) #1", first),
            ReviewEngineAttempt("Claude Opus 4.6 (Thinking) #2", second),
            ReviewEngineAttempt("claude-opus-4-8", claude),
        )
    )

    with pytest.raises(ReviewEngineError) as raised:
        await engine.review(_pr(), _dump())

    assert [detail.model_label for detail in raised.value.limit_details] == [
        "Claude Opus 4.6 (Thinking) #1"
    ]
    assert raised.value.non_limit_failure_message == (
        "Claude Opus 4.6 (Thinking) #2: agy --print timed out after 600s"
    )
    assert claude.calls == 0


@pytest.mark.asyncio
async def test_chain_starts_next_review_from_last_successful_rotating_slot() -> None:
    first = _SequenceEngine(
        [
            ReviewEngineError("maximum context window exceeded"),
            ReviewResult(summary="first ok", event=ReviewEvent.APPROVE),
        ]
    )
    second = _SequenceEngine(
        [
            ReviewResult(summary="second ok", event=ReviewEvent.APPROVE),
            ReviewEngineError("maximum context window exceeded"),
        ]
    )
    third = _Engine(error=ReviewEngineError("maximum context window exceeded"))
    claude = _Engine(result=ReviewResult(summary="claude ok", event=ReviewEvent.APPROVE))
    engine = ModelLimitFallbackChainReviewEngine(
        (
            ReviewEngineAttempt("Claude Opus 4.6 (Thinking) #1", first),
            ReviewEngineAttempt("Claude Opus 4.6 (Thinking) #2", second),
            ReviewEngineAttempt("Claude Opus 4.6 (Thinking) #3", third),
            ReviewEngineAttempt("claude-opus-4-8", claude),
        ),
        rotating_attempt_start=0,
        rotating_attempt_count=3,
    )

    first_result = await engine.review(_pr(number=1), _dump())
    second_result = await engine.review(_pr(number=2), _dump())

    assert first_result.summary == "second ok"
    assert first_result.model_label == "Claude Opus 4.6 (Thinking) #2"
    assert second_result.summary == "first ok"
    assert second_result.model_label == "Claude Opus 4.6 (Thinking) #1"
    assert first.calls == 2
    assert second.calls == 2
    assert third.calls == 1
    assert claude.calls == 0
