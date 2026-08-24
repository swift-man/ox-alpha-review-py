import json

import httpx
import pytest

from reviewbot_common.domain import Finding, PullRequest, RepoRef, ReviewEvent, ReviewResult
from reviewbot_common.infrastructure import github_app_client
from reviewbot_common.infrastructure.github_app_client import (
    GitHubAppClient,
    _CachedToken,
    _finding_to_comment,
)
from reviewbot_common.interfaces import PullRequestNotReviewableError

_DEFAULT_BASE_REPO = object()


def test_cached_token_expires_five_minutes_before_github_expiry(
    monkeypatch,
) -> None:
    monkeypatch.setattr(github_app_client.time, "time", lambda: 1_000.0)
    token = _CachedToken(token="token", expires_at=1_000.0 + 301)

    assert token.is_valid()

    monkeypatch.setattr(github_app_client.time, "time", lambda: 1_001.0)

    assert not token.is_valid()


def test_finding_to_comment_keeps_leading_code_fence_on_own_line() -> None:
    finding = Finding(
        path="src/app.py",
        line=3,
        body='```python\nallowed = ("PATH", "HOME")\n```',
        severity="major",
    )

    comment = _finding_to_comment(finding)  # noqa: SLF001

    assert comment["body"].startswith("[Major]\n```python")


def test_finding_to_comment_keeps_indented_leading_code_fence_on_own_line() -> None:
    finding = Finding(
        path="src/app.py",
        line=3,
        body='  ```python\nallowed = ("PATH", "HOME")\n```',
        severity="major",
    )

    comment = _finding_to_comment(finding)  # noqa: SLF001

    assert comment["body"].startswith("[Major]\n  ```python")


@pytest.mark.asyncio
async def test_fetch_pull_request_uses_base_clone_url_when_head_repo_is_null() -> None:
    async with httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(_mock_pr_transport(head_repo=None)),
    ) as http_client:
        client = GitHubAppClient(
            app_id=1,
            private_key_pem="private-key",
            http_client=http_client,
        )

        pr = await client._fetch_pull_request(  # noqa: SLF001
            RepoRef(owner="owner", name="repo"),
            7,
            42,
            "token",
        )

    assert pr.clone_url == "https://github.com/owner/repo.git"
    assert pr.changed_files == ("src/app.py",)


@pytest.mark.asyncio
async def test_fetch_pull_request_rejects_payload_without_checkout_clone_url() -> None:
    async with httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(_mock_pr_transport(head_repo=None, base_repo=None)),
    ) as http_client:
        client = GitHubAppClient(
            app_id=1,
            private_key_pem="private-key",
            http_client=http_client,
        )

        with pytest.raises(PullRequestNotReviewableError, match="no checkout clone_url"):
            await client._fetch_pull_request(  # noqa: SLF001
                RepoRef(owner="owner", name="repo"),
                7,
                42,
                "token",
            )


@pytest.mark.asyncio
async def test_request_returns_empty_dict_for_non_json_response() -> None:
    async with httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=b"not json")),
    ) as http_client:
        client = GitHubAppClient(
            app_id=1,
            private_key_pem="private-key",
            http_client=http_client,
        )

        data = await client._request("GET", "/bad-json", auth="token token")  # noqa: SLF001

    assert data == {}


@pytest.mark.asyncio
async def test_graphql_raises_for_non_json_response() -> None:
    async with httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=b"not json")),
    ) as http_client:
        client = GitHubAppClient(
            app_id=1,
            private_key_pem="private-key",
            http_client=http_client,
        )

        with pytest.raises(github_app_client._GraphQLError, match="non-JSON"):  # noqa: SLF001
            await client._graphql("query { viewer { login } }", {}, auth="token token")  # noqa: SLF001


@pytest.mark.asyncio
async def test_graphql_raises_for_non_object_json_response() -> None:
    async with httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=[])),
    ) as http_client:
        client = GitHubAppClient(
            app_id=1,
            private_key_pem="private-key",
            http_client=http_client,
        )

        with pytest.raises(github_app_client._GraphQLError, match="non-object"):
            await client._graphql("query { viewer { login } }", {}, auth="token token")  # noqa: SLF001


@pytest.mark.asyncio
async def test_post_review_refreshes_installation_token_after_bad_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_auths: list[str] = []
    token_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        if request.url.path == "/app/installations/42/access_tokens":
            token_requests += 1
            assert request.headers["authorization"] == "Bearer app-jwt"
            return httpx.Response(
                201,
                json={"token": "fresh-token", "expires_at": "2099-01-01T00:00:00Z"},
            )
        if request.url.path == "/repos/owner/repo/pulls/7/reviews":
            review_auths.append(request.headers["authorization"])
            if len(review_auths) == 1:
                return httpx.Response(401, json={"message": "Bad credentials"})
            return httpx.Response(200, json={"id": 123})
        return httpx.Response(404, json={"message": "not found"})

    async with httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = GitHubAppClient(
            app_id=1,
            private_key_pem="private-key",
            http_client=http_client,
        )
        monkeypatch.setattr(client, "_app_jwt", lambda: "app-jwt")
        client._token_cache[42] = _CachedToken(  # noqa: SLF001
            "stale-token",
            github_app_client.time.time() + 3_600,
        )

        await client.post_review(_review_pr(), _review_result())

    assert review_auths == ["token stale-token", "token fresh-token"]
    assert token_requests == 1


@pytest.mark.asyncio
async def test_post_review_uses_result_model_label_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/42/access_tokens":
            return httpx.Response(
                201,
                json={"token": "fresh-token", "expires_at": "2099-01-01T00:00:00Z"},
            )
        if request.url.path == "/repos/owner/repo/pulls/7/reviews":
            posted_payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"id": 123})
        return httpx.Response(404, json={"message": "not found"})

    async with httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = GitHubAppClient(
            app_id=1,
            private_key_pem="private-key",
            http_client=http_client,
            review_model_label="primary-model",
        )
        monkeypatch.setattr(client, "_app_jwt", lambda: "app-jwt")

        await client.post_review(
            _review_pr(),
            _review_result(model_label='fallback</code><b data-x="1">&'),
        )

    assert "<code>fallback&lt;/code&gt;&lt;b data-x=&quot;1&quot;&gt;&amp;</code>" in str(
        posted_payloads[0]["body"]
    )
    assert "primary-model" not in str(posted_payloads[0]["body"])


@pytest.mark.asyncio
async def test_fetch_pull_request_head_sha_refreshes_installation_token_after_bad_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head_auths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/42/access_tokens":
            return httpx.Response(
                201,
                json={"token": "fresh-token", "expires_at": "2099-01-01T00:00:00Z"},
            )
        if request.url.path == "/repos/owner/repo/pulls/7":
            head_auths.append(request.headers["authorization"])
            if len(head_auths) == 1:
                return httpx.Response(401, json={"message": "Bad credentials"})
            return httpx.Response(200, json={"head": {"sha": "fresh-head-sha"}})
        return httpx.Response(404, json={"message": "not found"})

    async with httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = GitHubAppClient(
            app_id=1,
            private_key_pem="private-key",
            http_client=http_client,
        )
        monkeypatch.setattr(client, "_app_jwt", lambda: "app-jwt")
        client._token_cache[42] = _CachedToken(  # noqa: SLF001
            "stale-token",
            github_app_client.time.time() + 3_600,
        )

        head_sha = await client.fetch_pull_request_head_sha(_review_pr())

    assert head_sha == "fresh-head-sha"
    assert head_auths == ["token stale-token", "token fresh-token"]


@pytest.mark.asyncio
async def test_post_review_refreshes_token_when_422_fallback_hits_bad_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_auths: list[str] = []
    review_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/42/access_tokens":
            return httpx.Response(
                201,
                json={"token": "fresh-token", "expires_at": "2099-01-01T00:00:00Z"},
            )
        if request.url.path == "/repos/owner/repo/pulls/7/reviews":
            review_auths.append(request.headers["authorization"])
            review_payloads.append(json.loads(request.content))
            if len(review_auths) == 1:
                return httpx.Response(422, json={"message": "Validation Failed"})
            if len(review_auths) == 2:
                return httpx.Response(401, json={"message": "Bad credentials"})
            return httpx.Response(200, json={"id": 123})
        return httpx.Response(404, json={"message": "not found"})

    async with httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = GitHubAppClient(
            app_id=1,
            private_key_pem="private-key",
            http_client=http_client,
        )
        monkeypatch.setattr(client, "_app_jwt", lambda: "app-jwt")
        client._token_cache[42] = _CachedToken(  # noqa: SLF001
            "stale-token",
            github_app_client.time.time() + 3_600,
        )

        await client.post_review(
            _review_pr(),
            _review_result(
                findings=(
                    Finding(
                        path="src/app.py",
                        line=1,
                        body="검증 가능한 문제가 있습니다.",
                        severity="major",
                    ),
                )
            ),
        )

    assert review_auths == ["token stale-token", "token stale-token", "token fresh-token"]
    assert review_payloads[0]["comments"]
    assert review_payloads[1]["comments"] == []
    assert review_payloads[2]["comments"] == []


@pytest.mark.asyncio
async def test_post_comment_refreshes_installation_token_after_bad_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comment_auths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/42/access_tokens":
            return httpx.Response(
                201,
                json={"token": "fresh-token", "expires_at": "2099-01-01T00:00:00Z"},
            )
        if request.url.path == "/repos/owner/repo/issues/7/comments":
            comment_auths.append(request.headers["authorization"])
            if len(comment_auths) == 1:
                return httpx.Response(401, json={"message": "Bad credentials"})
            return httpx.Response(201, json={"id": 456})
        return httpx.Response(404, json={"message": "not found"})

    async with httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = GitHubAppClient(
            app_id=1,
            private_key_pem="private-key",
            http_client=http_client,
        )
        monkeypatch.setattr(client, "_app_jwt", lambda: "app-jwt")
        client._token_cache[42] = _CachedToken(  # noqa: SLF001
            "stale-token",
            github_app_client.time.time() + 3_600,
        )

        await client.post_comment(_review_pr(), "diagnostic comment")

    assert comment_auths == ["token stale-token", "token fresh-token"]


@pytest.mark.asyncio
async def test_reply_to_review_comment_refreshes_installation_token_after_bad_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply_auths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/42/access_tokens":
            return httpx.Response(
                201,
                json={"token": "fresh-token", "expires_at": "2099-01-01T00:00:00Z"},
            )
        if request.url.path == "/repos/owner/repo/pulls/7/comments/99/replies":
            reply_auths.append(request.headers["authorization"])
            if len(reply_auths) == 1:
                return httpx.Response(401, json={"message": "Bad credentials"})
            return httpx.Response(201, json={"id": 789})
        return httpx.Response(404, json={"message": "not found"})

    async with httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = GitHubAppClient(
            app_id=1,
            private_key_pem="private-key",
            http_client=http_client,
        )
        monkeypatch.setattr(client, "_app_jwt", lambda: "app-jwt")
        client._token_cache[42] = _CachedToken(  # noqa: SLF001
            "stale-token",
            github_app_client.time.time() + 3_600,
        )

        await client.reply_to_review_comment(_review_pr(), 99, "follow-up")

    assert reply_auths == ["token stale-token", "token fresh-token"]


@pytest.mark.asyncio
async def test_review_thread_graphql_calls_refresh_installation_token_after_bad_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graphql_auths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/42/access_tokens":
            return httpx.Response(
                201,
                json={"token": "fresh-token", "expires_at": "2099-01-01T00:00:00Z"},
            )
        if request.url.path == "/graphql":
            graphql_auths.append(request.headers["authorization"])
            if len(graphql_auths) in {1, 3}:
                return httpx.Response(401, json={"message": "Bad credentials"})
            payload = json.loads(request.content)
            if "resolveReviewThread" in payload["query"]:
                return httpx.Response(
                    200,
                    json={"data": {"resolveReviewThread": {"thread": {"id": "T1"}}}},
                )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    "nodes": [],
                                }
                            }
                        }
                    }
                },
            )
        return httpx.Response(404, json={"message": "not found"})

    async with httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = GitHubAppClient(
            app_id=1,
            private_key_pem="private-key",
            http_client=http_client,
        )
        monkeypatch.setattr(client, "_app_jwt", lambda: "app-jwt")
        client._token_cache[42] = _CachedToken(  # noqa: SLF001
            "stale-token",
            github_app_client.time.time() + 3_600,
        )

        assert await client.list_review_threads(_review_pr(), 42) == ()
        client._token_cache[42] = _CachedToken(  # noqa: SLF001
            "stale-token",
            github_app_client.time.time() + 3_600,
        )
        await client.resolve_review_thread("T1", 42)

    assert graphql_auths == [
        "token stale-token",
        "token fresh-token",
        "token stale-token",
        "token fresh-token",
    ]


@pytest.mark.asyncio
async def test_fetch_review_history_refreshes_installation_token_after_bad_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history_auths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/42/access_tokens":
            return httpx.Response(
                201,
                json={"token": "fresh-token", "expires_at": "2099-01-01T00:00:00Z"},
            )
        if request.url.path in {
            "/repos/owner/repo/issues/7/comments",
            "/repos/owner/repo/pulls/7/comments",
            "/repos/owner/repo/pulls/7/reviews",
        }:
            history_auths.append(request.headers["authorization"])
            if request.headers["authorization"] == "token stale-token":
                return httpx.Response(401, json={"message": "Bad credentials"})
            return httpx.Response(200, json=_history_payload(request.url.path))
        return httpx.Response(404, json={"message": "not found"})

    async with httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = GitHubAppClient(
            app_id=1,
            private_key_pem="private-key",
            http_client=http_client,
        )
        monkeypatch.setattr(client, "_app_jwt", lambda: "app-jwt")
        client._token_cache[42] = _CachedToken(  # noqa: SLF001
            "stale-token",
            github_app_client.time.time() + 3_600,
        )

        history = await client.fetch_review_history(_review_pr(), 42)

    assert history_auths.count("token stale-token") == 3
    assert history_auths.count("token fresh-token") == 3
    assert [comment.kind for comment in history.comments] == [
        "issue",
        "inline",
        "review-summary",
    ]


def _review_pr() -> PullRequest:
    return PullRequest(
        repo=RepoRef(owner="owner", name="repo"),
        number=7,
        title="Test PR",
        body="",
        head_sha="abc123",
        head_ref="feature",
        base_sha="def456",
        base_ref="main",
        clone_url="https://github.com/owner/repo.git",
        changed_files=("src/app.py",),
        installation_id=42,
        is_draft=False,
        diff_right_lines={"src/app.py": frozenset({1})},
        diff_patches={"src/app.py": "@@ -0,0 +1 @@\n+print('hello')"},
    )


def _history_payload(path: str) -> list[dict[str, object]]:
    if path.endswith("/issues/7/comments"):
        return [
            {
                "user": {"login": "alice"},
                "body": "issue comment",
                "created_at": "2026-06-02T01:00:00Z",
            }
        ]
    if path.endswith("/pulls/7/comments"):
        return [
            {
                "id": 99,
                "user": {"login": "bob"},
                "body": "inline comment",
                "created_at": "2026-06-02T01:01:00Z",
                "path": "src/app.py",
                "line": 1,
            }
        ]
    return [
        {
            "user": {"login": "review-bot"},
            "body": "review summary",
            "submitted_at": "2026-06-02T01:02:00Z",
        }
    ]


def _review_result(
    findings: tuple[Finding, ...] = (),
    model_label: str | None = None,
) -> ReviewResult:
    return ReviewResult(
        summary="검토 결과입니다.",
        event=ReviewEvent.COMMENT,
        findings=findings,
        model_label=model_label,
    )


def _mock_pr_transport(
    *,
    head_repo: object = None,
    base_repo: object = _DEFAULT_BASE_REPO,
):
    if base_repo is _DEFAULT_BASE_REPO:
        base_repo = {"clone_url": "https://github.com/owner/repo.git"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/owner/repo/pulls/7":
            return httpx.Response(
                200,
                json={
                    "title": "Test",
                    "body": None,
                    "draft": False,
                    "head": {"sha": "head-sha", "ref": "feature", "repo": head_repo},
                    "base": {"sha": "base-sha", "ref": "main", "repo": base_repo},
                },
            )
        if request.url.path == "/repos/owner/repo/pulls/7/files":
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "src/app.py",
                        "status": "added",
                        "patch": "@@ -0,0 +1 @@\n+print('hello')",
                    }
                ],
            )
        return httpx.Response(404, json={"message": "not found"})

    return handler
