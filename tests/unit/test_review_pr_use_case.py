from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reviewbot_common.application.review_pr_use_case import (
    _FAILURE_FULL_THEN_DIFF,
    ReviewPullRequestUseCase,
    _engine_failure_message,
    _filter_findings_to_diff,
)
from reviewbot_common.domain import (
    FileDump,
    FileEntry,
    Finding,
    PullRequest,
    RepoRef,
    ReviewComment,
    ReviewEvent,
    ReviewHistory,
    ReviewResult,
    ReviewThread,
    TokenBudget,
)
from reviewbot_common.infrastructure.diff_context_collector import DiffContextCollector
from reviewbot_common.interfaces import ModelLimitDetail, ReviewEngineError

_TEST_BOT_LOGIN = "claude-review-bot[bot]"
_TEST_INSTALLATION_ID = 42
_TEST_CREATED_AT = datetime(2026, 6, 7, tzinfo=UTC)
_HEAD_SHA_A = "a" * 40
_HEAD_SHA_B = "b" * 40


def test_right_side_filter_preserves_rejected_findings_in_review_body() -> None:
    kept = Finding("src/app.py", 1, "유효한 인라인 지적", "major")
    dropped = Finding("src/app.py", 2, "diff 밖 지적", "minor")
    result = ReviewResult(
        summary="검토 결과",
        event=ReviewEvent.COMMENT,
        findings=(kept, dropped),
    )

    filtered = _filter_findings_to_diff(
        result,
        {"src/app.py": frozenset({1})},
        "owner/repo",
        7,
    )

    assert filtered.findings == (kept,)
    assert filtered.dropped_findings == (dropped,)
    assert "`src/app.py:2`" in filtered.render_body()
    assert "diff 밖 지적" in filtered.render_body()


def _pr(head_sha: str = _HEAD_SHA_A) -> PullRequest:
    return PullRequest(
        repo=RepoRef(owner="owner", name="repo"),
        number=7,
        title="Test PR",
        body="",
        head_sha=head_sha,
        head_ref="feature",
        base_sha="base-sha",
        base_ref="main",
        clone_url="https://github.com/owner/repo.git",
        changed_files=("src/app.py",),
        installation_id=_TEST_INSTALLATION_ID,
        is_draft=False,
        diff_right_lines={"src/app.py": frozenset({1})},
        diff_patches={"src/app.py": "@@ -0,0 +1 @@\n+print('hello')"},
    )


def _history_comment(author_login: str, body: str) -> ReviewComment:
    return ReviewComment(
        author_login=author_login,
        kind="issue",
        body=body,
        created_at=_TEST_CREATED_AT,
    )


def _history_review_summary(author_login: str, body: str) -> ReviewComment:
    return ReviewComment(
        author_login=author_login,
        kind="review-summary",
        body=body,
        created_at=_TEST_CREATED_AT,
    )


def _history_inline_comment(author_login: str, body: str) -> ReviewComment:
    return ReviewComment(
        author_login=author_login,
        kind="inline",
        body=body,
        created_at=_TEST_CREATED_AT,
        comment_id=123,
        path="src/app.py",
        line=1,
    )


def test_engine_failure_message_renders_model_limit_reset_details() -> None:
    exc = ReviewEngineError(
        "model-limit fallback failed",
        limit_details=(
            ModelLimitDetail(
                model_label="claude-opus-4-8",
                message="You've hit your session limit · resets 11:30pm (Asia/Seoul)",
                reset_hint="11:30pm (Asia/Seoul)",
            ),
            ModelLimitDetail(
                model_label="Claude Opus 4.6 (Thinking)",
                message="RESOURCE_EXHAUSTED. Resets in 50h46m4s.",
                reset_hint="50h46m4s",
            ),
        ),
    )
    body = _engine_failure_message(
        _pr(),
        FileDump(entries=(), total_chars=0),
        exc,
        failure_mode=_FAILURE_FULL_THEN_DIFF,
        engine_label="Claude Review",
        max_input_tokens_env="CLAUDE_MAX_INPUT_TOKENS",
        model_env="CLAUDE_MODEL",
        enable_diff_fallback_env="CLAUDE_ENABLE_DIFF_FALLBACK",
    )

    assert "**모델 한도 해제 정보**" in body
    assert "`claude-opus-4-8`: `11:30pm (Asia/Seoul)`" in body
    assert "`Claude Opus 4.6 (Thinking)`: `50h46m4s`" in body
    assert "실패 원인" not in body
    assert "You've hit your session limit" not in body
    assert "RESOURCE_EXHAUSTED" not in body


def test_engine_failure_message_hides_model_limit_error_messages() -> None:
    exc = ReviewEngineError(
        "model-limit fallback failed",
        limit_details=(
            ModelLimitDetail(
                model_label="Claude `debug`",
                message="first line with `tick`\nsecond line with ``` fence",
                reset_hint="reset `soon`",
            ),
        ),
    )
    body = _engine_failure_message(
        _pr(),
        FileDump(entries=(), total_chars=0),
        exc,
        failure_mode=_FAILURE_FULL_THEN_DIFF,
        engine_label="Claude Review",
        max_input_tokens_env="CLAUDE_MAX_INPUT_TOKENS",
        model_env="CLAUDE_MODEL",
        enable_diff_fallback_env="CLAUDE_ENABLE_DIFF_FALLBACK",
    )

    assert "`` Claude `debug` ``: `` reset `soon` ``" in body
    assert "실패 원인" not in body
    assert "에러 메시지" not in body
    assert "first line with `tick`" not in body
    assert "second line with ``` fence" not in body


def test_engine_failure_message_shows_non_limit_fallback_failure_cause() -> None:
    exc = ReviewEngineError(
        "model-limit fallback failed",
        limit_details=(
            ModelLimitDetail(
                model_label="claude-opus-4-8",
                message="You've hit your session limit · resets 11:30pm (Asia/Seoul)",
                reset_hint="11:30pm (Asia/Seoul)",
            ),
        ),
        non_limit_failure_message="Claude Opus 4.6 (Thinking): agy timed out after 600s",
    )
    body = _engine_failure_message(
        _pr(),
        FileDump(entries=(), total_chars=0),
        exc,
        failure_mode=_FAILURE_FULL_THEN_DIFF,
        engine_label="Claude Review",
        max_input_tokens_env="CLAUDE_MAX_INPUT_TOKENS",
        model_env="CLAUDE_MODEL",
        enable_diff_fallback_env="CLAUDE_ENABLE_DIFF_FALLBACK",
    )

    assert "`claude-opus-4-8`: `11:30pm (Asia/Seoul)`" in body
    assert "You've hit your session limit" not in body
    assert "- 실패 원인:" in body
    assert "Claude Opus 4.6 (Thinking): agy timed out after 600s" in body


class _FakeGitHub:
    def __init__(
        self,
        existing_comments: tuple[ReviewComment, ...] = (),
        current_head_sha: str | None = None,
    ) -> None:
        self._existing_comments = existing_comments
        self._current_head_sha = current_head_sha
        self.posted_comments: list[str] = []
        self.posted_reviews: list[ReviewResult] = []
        self.head_sha_requests: list[PullRequest] = []

    async def fetch_pull_request(
        self,
        repo: RepoRef,
        number: int,
        installation_id: int,
    ) -> PullRequest:
        raise AssertionError("fetch_pull_request should not be called")

    async def post_review(self, pr: PullRequest, result: ReviewResult) -> None:
        self.posted_reviews.append(result)

    async def fetch_pull_request_head_sha(self, pr: PullRequest) -> str:
        self.head_sha_requests.append(pr)
        return self._current_head_sha if self._current_head_sha is not None else pr.head_sha

    async def get_installation_token(self, installation_id: int) -> str:
        assert installation_id == _TEST_INSTALLATION_ID
        return "installation-token"

    async def fetch_review_history(
        self,
        pr: PullRequest,
        installation_id: int,
    ) -> ReviewHistory:
        assert installation_id == pr.installation_id
        posted = tuple(_history_comment(_TEST_BOT_LOGIN, body) for body in self.posted_comments)
        return ReviewHistory(
            comments=self._existing_comments + posted,
        )

    async def post_comment(self, pr: PullRequest, body: str) -> None:
        self.posted_comments.append(body)

    async def list_review_threads(
        self,
        pr: PullRequest,
        installation_id: int,
    ) -> tuple[ReviewThread, ...]:
        raise AssertionError("list_review_threads should not be called")

    async def reply_to_review_comment(
        self,
        pr: PullRequest,
        comment_id: int,
        body: str,
    ) -> None:
        raise AssertionError("reply_to_review_comment should not be called")

    async def resolve_review_thread(
        self,
        thread_id: str,
        installation_id: int,
    ) -> None:
        raise AssertionError("resolve_review_thread should not be called")


class _FakeRepoFetcher:
    def __init__(self, root: Path) -> None:
        self._root = root
        self.sessions = 0

    @asynccontextmanager
    async def session(
        self,
        pr: PullRequest,
        installation_token: str,
    ) -> AsyncIterator[Path]:
        self.sessions += 1
        yield self._root

    async def head_sha(self, repo_path: Path) -> str:
        return "head-sha"


class _FakeFileCollector:
    async def collect(
        self,
        root: Path,
        changed_files: tuple[str, ...],
        budget: TokenBudget,
    ) -> FileDump:
        return FileDump(
            entries=(
                FileEntry(
                    path="src/app.py",
                    content="1| print('hello')",
                    size_bytes=16,
                    is_changed=True,
                ),
            ),
            total_chars=16,
            budget=budget,
        )


class _LimitFailingEngine:
    def __init__(self) -> None:
        self.calls = 0

    async def review(
        self,
        pr: PullRequest,
        dump: FileDump,
        *,
        history: ReviewHistory | None = None,
    ) -> None:
        self.calls += 1
        raise ReviewEngineError("You've hit your weekly limit · resets 8pm")


class _MixedLimitFailingEngine:
    def __init__(self) -> None:
        self.calls = 0

    async def review(
        self,
        pr: PullRequest,
        dump: FileDump,
        *,
        history: ReviewHistory | None = None,
    ) -> None:
        _ = (pr, dump, history)
        self.calls += 1
        raise ReviewEngineError(
            "model-limit fallback chain failed",
            limit_details=(
                ModelLimitDetail(
                    model_label="Claude Opus 4.6 (Thinking) #1",
                    message="RESOURCE_EXHAUSTED. Resets in 1h.",
                    reset_hint="1h",
                ),
            ),
            non_limit_failure_message="Claude Opus 4.6 (Thinking) #2: timed out after 600s",
        )


class _SuccessfulEngine:
    def __init__(self) -> None:
        self.calls = 0

    async def review(
        self,
        pr: PullRequest,
        dump: FileDump,
        *,
        history: ReviewHistory | None = None,
    ) -> ReviewResult:
        self.calls += 1
        return ReviewResult(summary="문제 없습니다.", event=ReviewEvent.COMMENT)


class _NonRetryableFailingEngine:
    def __init__(self) -> None:
        self.calls = 0

    async def review(
        self,
        pr: PullRequest,
        dump: FileDump,
        *,
        history: ReviewHistory | None = None,
    ) -> None:
        self.calls += 1
        raise ReviewEngineError(
            "Ox Alpha 결제 안전장치가 추론을 차단했습니다.",
            non_limit_failure_message="Ox Alpha 결제 안전장치가 추론을 차단했습니다.",
            allow_diff_fallback=False,
        )


@pytest.mark.asyncio
async def test_non_retryable_engine_failure_never_enters_diff_fallback(tmp_path: Path) -> None:
    github = _FakeGitHub()
    engine = _NonRetryableFailingEngine()
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=_FakeRepoFetcher(tmp_path),
        file_collector=_FakeFileCollector(),
        engine=engine,
        max_input_tokens=10_000,
        diff_context_collector=DiffContextCollector(overhead_estimator=lambda pr, dump, history: 0),
        bot_login=_TEST_BOT_LOGIN,
    )

    await use_case.execute(_pr())

    assert engine.calls == 1
    assert len(github.posted_comments) == 1
    assert "결제 안전장치" in github.posted_comments[0]


@pytest.mark.asyncio
async def test_review_is_not_posted_when_head_changes_before_post(tmp_path: Path) -> None:
    github = _FakeGitHub(current_head_sha=_HEAD_SHA_B)
    repo_fetcher = _FakeRepoFetcher(tmp_path)
    engine = _SuccessfulEngine()
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=repo_fetcher,
        file_collector=_FakeFileCollector(),
        engine=engine,
        max_input_tokens=10_000,
        bot_login=_TEST_BOT_LOGIN,
    )

    await use_case.execute(_pr(_HEAD_SHA_A))

    assert github.posted_reviews == []
    assert github.posted_comments == []
    assert len(github.head_sha_requests) == 1
    assert engine.calls == 1
    assert repo_fetcher.sessions == 1


@pytest.mark.asyncio
async def test_review_head_check_ignores_case_and_space(tmp_path: Path) -> None:
    github = _FakeGitHub(current_head_sha=f"  {_HEAD_SHA_A.upper()}  ")
    repo_fetcher = _FakeRepoFetcher(tmp_path)
    engine = _SuccessfulEngine()
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=repo_fetcher,
        file_collector=_FakeFileCollector(),
        engine=engine,
        max_input_tokens=10_000,
        bot_login=_TEST_BOT_LOGIN,
    )

    await use_case.execute(_pr(_HEAD_SHA_A))

    assert len(github.posted_reviews) == 1
    assert github.posted_comments == []
    assert len(github.head_sha_requests) == 1
    assert engine.calls == 1
    assert repo_fetcher.sessions == 1


@pytest.mark.asyncio
async def test_engine_failure_comment_is_not_posted_when_head_changes_before_post(
    tmp_path: Path,
) -> None:
    github = _FakeGitHub(current_head_sha=_HEAD_SHA_B)
    repo_fetcher = _FakeRepoFetcher(tmp_path)
    engine = _LimitFailingEngine()
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=repo_fetcher,
        file_collector=_FakeFileCollector(),
        engine=engine,
        max_input_tokens=10_000,
        bot_login=_TEST_BOT_LOGIN,
    )

    await use_case.execute(_pr(_HEAD_SHA_A))

    assert github.posted_comments == []
    assert github.posted_reviews == []
    assert len(github.head_sha_requests) == 1
    assert engine.calls == 1
    assert repo_fetcher.sessions == 1


@pytest.mark.asyncio
async def test_model_limit_notice_is_not_reposted_for_same_head(tmp_path: Path) -> None:
    github = _FakeGitHub()
    repo_fetcher = _FakeRepoFetcher(tmp_path)
    engine = _LimitFailingEngine()
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=repo_fetcher,
        file_collector=_FakeFileCollector(),
        engine=engine,
        max_input_tokens=10_000,
        bot_login=_TEST_BOT_LOGIN,
    )

    await use_case.execute(_pr(_HEAD_SHA_A))
    await use_case.execute(_pr(_HEAD_SHA_A))

    assert len(github.posted_comments) == 1
    assert f"<!-- reviewbot:model-limit-notice head={_HEAD_SHA_A} -->" in github.posted_comments[0]
    assert engine.calls == 1
    assert repo_fetcher.sessions == 1


@pytest.mark.asyncio
async def test_model_limit_notice_is_not_reposted_after_head_changes_while_limit_active(
    tmp_path: Path,
) -> None:
    github = _FakeGitHub()
    repo_fetcher = _FakeRepoFetcher(tmp_path)
    engine = _LimitFailingEngine()
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=repo_fetcher,
        file_collector=_FakeFileCollector(),
        engine=engine,
        max_input_tokens=10_000,
        bot_login=_TEST_BOT_LOGIN,
    )

    await use_case.execute(_pr(_HEAD_SHA_A))
    await use_case.execute(_pr(_HEAD_SHA_B))

    assert len(github.posted_comments) == 1
    assert f"<!-- reviewbot:model-limit-notice head={_HEAD_SHA_A} -->" in github.posted_comments[0]
    assert engine.calls == 2
    assert repo_fetcher.sessions == 2


@pytest.mark.asyncio
async def test_mixed_limit_failure_posts_failure_cause_while_limit_notice_active(
    tmp_path: Path,
) -> None:
    github = _FakeGitHub(
        existing_comments=(
            _history_comment(
                _TEST_BOT_LOGIN,
                f"<!-- reviewbot:model-limit-notice head={_HEAD_SHA_A} -->",
            ),
        ),
    )
    repo_fetcher = _FakeRepoFetcher(tmp_path)
    engine = _MixedLimitFailingEngine()
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=repo_fetcher,
        file_collector=_FakeFileCollector(),
        engine=engine,
        max_input_tokens=10_000,
        bot_login=_TEST_BOT_LOGIN,
    )

    await use_case.execute(_pr(_HEAD_SHA_B))

    assert len(github.posted_comments) == 1
    assert "- 실패 원인:" in github.posted_comments[0]
    assert "timed out after 600s" in github.posted_comments[0]
    assert f"<!-- reviewbot:model-limit-notice head={_HEAD_SHA_B} -->" in github.posted_comments[0]
    assert engine.calls == 1
    assert repo_fetcher.sessions == 1


@pytest.mark.asyncio
async def test_mixed_limit_failure_is_not_reposted_for_same_head(tmp_path: Path) -> None:
    github = _FakeGitHub(
        existing_comments=(
            _history_comment(
                _TEST_BOT_LOGIN,
                "- 실패 원인:\n```\ntimed out after 600s\n```\n\n"
                f"<!-- reviewbot:model-limit-notice head={_HEAD_SHA_A} -->",
            ),
        ),
    )
    repo_fetcher = _FakeRepoFetcher(tmp_path)
    engine = _MixedLimitFailingEngine()
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=repo_fetcher,
        file_collector=_FakeFileCollector(),
        engine=engine,
        max_input_tokens=10_000,
        bot_login=_TEST_BOT_LOGIN,
    )

    await use_case.execute(_pr(_HEAD_SHA_A))

    assert github.posted_comments == []
    assert engine.calls == 0
    assert repo_fetcher.sessions == 0


@pytest.mark.asyncio
async def test_model_limit_notice_can_be_posted_after_successful_bot_review(
    tmp_path: Path,
) -> None:
    github = _FakeGitHub(
        existing_comments=(
            _history_comment(
                _TEST_BOT_LOGIN,
                f"<!-- reviewbot:model-limit-notice head={_HEAD_SHA_A} -->",
            ),
            _history_review_summary(_TEST_BOT_LOGIN, "정상 리뷰가 완료되었습니다."),
        ),
    )
    repo_fetcher = _FakeRepoFetcher(tmp_path)
    engine = _LimitFailingEngine()
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=repo_fetcher,
        file_collector=_FakeFileCollector(),
        engine=engine,
        max_input_tokens=10_000,
        bot_login=_TEST_BOT_LOGIN,
    )

    await use_case.execute(_pr(_HEAD_SHA_B))

    assert len(github.posted_comments) == 1
    assert f"<!-- reviewbot:model-limit-notice head={_HEAD_SHA_B} -->" in github.posted_comments[0]
    assert engine.calls == 1
    assert repo_fetcher.sessions == 1


@pytest.mark.asyncio
async def test_model_limit_notice_can_be_posted_after_successful_bot_inline_review(
    tmp_path: Path,
) -> None:
    github = _FakeGitHub(
        existing_comments=(
            _history_comment(
                _TEST_BOT_LOGIN,
                f"<!-- reviewbot:model-limit-notice head={_HEAD_SHA_A} -->",
            ),
            _history_inline_comment(_TEST_BOT_LOGIN, "정상 inline 리뷰가 완료되었습니다."),
        ),
    )
    repo_fetcher = _FakeRepoFetcher(tmp_path)
    engine = _LimitFailingEngine()
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=repo_fetcher,
        file_collector=_FakeFileCollector(),
        engine=engine,
        max_input_tokens=10_000,
        bot_login=_TEST_BOT_LOGIN,
    )

    await use_case.execute(_pr(_HEAD_SHA_B))

    assert len(github.posted_comments) == 1
    assert f"<!-- reviewbot:model-limit-notice head={_HEAD_SHA_B} -->" in github.posted_comments[0]
    assert engine.calls == 1
    assert repo_fetcher.sessions == 1


@pytest.mark.asyncio
async def test_model_limit_notice_marker_from_other_author_does_not_skip(
    tmp_path: Path,
) -> None:
    github = _FakeGitHub(
        existing_comments=(
            _history_comment(
                "swift-man",
                f"<!-- reviewbot:model-limit-notice head={_HEAD_SHA_A} -->",
            ),
        ),
    )
    repo_fetcher = _FakeRepoFetcher(tmp_path)
    engine = _LimitFailingEngine()
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=repo_fetcher,
        file_collector=_FakeFileCollector(),
        engine=engine,
        max_input_tokens=10_000,
        bot_login=_TEST_BOT_LOGIN,
    )

    await use_case.execute(_pr(_HEAD_SHA_A))

    assert len(github.posted_comments) == 1
    assert engine.calls == 1
    assert repo_fetcher.sessions == 1


@pytest.mark.asyncio
async def test_model_limit_notice_marker_sanitizes_unexpected_head_sha(
    tmp_path: Path,
) -> None:
    github = _FakeGitHub()
    engine = _LimitFailingEngine()
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=_FakeRepoFetcher(tmp_path),
        file_collector=_FakeFileCollector(),
        engine=engine,
        max_input_tokens=10_000,
        bot_login=_TEST_BOT_LOGIN,
    )

    await use_case.execute(_pr("bad-->head"))

    assert len(github.posted_comments) == 1
    assert "bad-->head" not in github.posted_comments[0]
    assert "<!-- reviewbot:model-limit-notice head=sha256-" in github.posted_comments[0]
