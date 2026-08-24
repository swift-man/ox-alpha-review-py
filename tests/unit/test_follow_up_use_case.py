import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from reviewbot_common.application import follow_up_use_case
from reviewbot_common.application.follow_up_use_case import FollowUpReviewUseCase
from reviewbot_common.domain import PullRequest, RepoRef, ReviewThread


def test_follow_up_uses_injected_api_semaphore() -> None:
    semaphore = asyncio.Semaphore(1)
    use_case = FollowUpReviewUseCase(
        github=object(),  # type: ignore[arg-type]
        repo_fetcher=object(),  # type: ignore[arg-type]
        bot_user_login="claude-review-bot[bot]",
        api_semaphore=semaphore,
    )

    assert use_case._api_semaphore is semaphore  # noqa: SLF001


def _pr() -> PullRequest:
    return PullRequest(
        repo=RepoRef(owner="owner", name="repo"),
        number=1,
        title="Test",
        body="",
        head_sha="head-sha",
        head_ref="feature",
        base_sha="base-sha",
        base_ref="main",
        clone_url="https://github.com/owner/repo.git",
        changed_files=("a.py",),
        installation_id=42,
        is_draft=False,
    )


def _thread(thread_id: str) -> ReviewThread:
    return ReviewThread(
        id=thread_id,
        is_resolved=False,
        root_comment_id=100,
        root_author_login="claude-review-bot[bot]",
        path="a.py",
        line=1,
        commit_id="old-sha",
        body="body",
        has_non_root_author_reply=False,
        has_followup_marker=False,
    )


class _FakeGitHub:
    def __init__(self, threads: list[ReviewThread]) -> None:
        self._threads = threads

    async def list_review_threads(
        self, pr: PullRequest, installation_id: int
    ) -> list[ReviewThread]:
        return self._threads

    async def get_installation_token(self, installation_id: int) -> str:
        return "token"

    async def resolve_review_thread(self, thread_id: str, installation_id: int) -> None:
        return None

    async def reply_to_review_comment(self, pr: PullRequest, comment_id: int, body: str) -> None:
        return None


class _FakeRepoFetcher:
    def __init__(self, repo_path: Path) -> None:
        self._repo_path = repo_path

    @asynccontextmanager
    async def session(self, pr: PullRequest, installation_token: str) -> AsyncIterator[Path]:
        yield self._repo_path

    async def head_sha(self, repo_path: Path) -> str:
        return "head-sha"


@pytest.mark.asyncio
async def test_follow_up_classifies_candidates_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidates = [_thread("thread-1"), _thread("thread-2")]
    started = 0
    all_started = asyncio.Event()

    async def fake_to_thread(
        func: object,
        thread: ReviewThread,
        repo_path: Path,
    ) -> follow_up_use_case._Action:
        nonlocal started
        started += 1
        if started == len(candidates):
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=0.2)
        return follow_up_use_case._Action(reply_body=f"resolved {thread.id}")

    monkeypatch.setattr(follow_up_use_case.asyncio, "to_thread", fake_to_thread)
    use_case = FollowUpReviewUseCase(
        github=_FakeGitHub(candidates),
        repo_fetcher=_FakeRepoFetcher(tmp_path),
        bot_user_login="claude-review-bot[bot]",
    )

    await asyncio.wait_for(use_case.execute(_pr()), timeout=1)

    assert started == len(candidates)
