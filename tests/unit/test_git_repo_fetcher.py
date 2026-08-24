import asyncio
from collections.abc import Mapping
from pathlib import Path

import pytest

from reviewbot_common.domain import PullRequest, RepoRef
from reviewbot_common.infrastructure import git_repo_fetcher
from reviewbot_common.infrastructure.git_repo_fetcher import GitRepoFetcher


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
        changed_files=(),
        installation_id=42,
        is_draft=False,
        diff_right_lines={},
        diff_patches={},
    )


@pytest.mark.asyncio
async def test_checkout_restores_remote_url_when_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "owner" / "repo"
    (repo_path / ".git").mkdir(parents=True)
    restored: list[tuple[Path, str]] = []

    async def fake_run(
        cmd: list[str],
        *,
        check: bool = True,
        extra_env: Mapping[str, str] | None = None,
        timeout_sec: float = git_repo_fetcher.DEFAULT_GIT_TIMEOUT_SEC,
    ) -> None:
        raise asyncio.CancelledError

    def fake_restore(
        path: Path,
        clone_url: str,
        *,
        timeout_sec: float = git_repo_fetcher.DEFAULT_GIT_TIMEOUT_SEC,
    ) -> None:
        restored.append((path, clone_url))

    monkeypatch.setattr(git_repo_fetcher, "_run", fake_run)
    monkeypatch.setattr(git_repo_fetcher, "_restore_remote_url_sync", fake_restore)
    fetcher = GitRepoFetcher(cache_dir=tmp_path)
    pr = _pr()

    with pytest.raises(asyncio.CancelledError):
        await fetcher._checkout_locked(pr, "installation-token")  # noqa: SLF001

    assert restored == [(repo_path, pr.clone_url)]


def test_git_error_redaction_masks_url_userinfo_with_common_token_chars() -> None:
    text = "fatal https://x-access-token:ghs_Amd.123@github.com/owner/repo failed"

    assert (
        git_repo_fetcher._mask_tokens_in_text(text)  # noqa: SLF001
        == "fatal https://***@github.com/owner/repo failed"
    )


def test_git_error_redaction_masks_authorization_header() -> None:
    text = "trace Authorization: Basic abc123==\nfatal: failed"

    assert (
        git_repo_fetcher._mask_tokens_in_text(text)  # noqa: SLF001
        == "trace Authorization: Basic ***\nfatal: failed"
    )


@pytest.mark.asyncio
async def test_checkout_removes_incomplete_cache_when_initial_clone_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "owner" / "repo"

    async def fake_run(
        cmd: list[str],
        *,
        check: bool = True,
        extra_env: Mapping[str, str] | None = None,
        timeout_sec: float = git_repo_fetcher.DEFAULT_GIT_TIMEOUT_SEC,
    ) -> None:
        if cmd[:2] == ["git", "clone"]:
            (repo_path / ".git").mkdir(parents=True)
            raise RuntimeError("clone failed")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(git_repo_fetcher, "_run", fake_run)
    fetcher = GitRepoFetcher(cache_dir=tmp_path)

    with pytest.raises(RuntimeError, match="clone failed"):
        await fetcher._checkout_locked(_pr(), "installation-token")  # noqa: SLF001

    assert not repo_path.exists()


@pytest.mark.asyncio
async def test_checkout_uses_extra_header_without_token_in_git_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "owner" / "repo"
    calls: list[tuple[list[str], Mapping[str, str] | None]] = []
    restored: list[str] = []

    async def fake_run(
        cmd: list[str],
        *,
        check: bool = True,
        extra_env: Mapping[str, str] | None = None,
        timeout_sec: float = git_repo_fetcher.DEFAULT_GIT_TIMEOUT_SEC,
    ) -> None:
        calls.append((cmd, extra_env))
        if cmd[:2] == ["git", "clone"]:
            (repo_path / ".git").mkdir(parents=True)

    def fake_restore(
        path: Path,
        clone_url: str,
        *,
        timeout_sec: float = git_repo_fetcher.DEFAULT_GIT_TIMEOUT_SEC,
    ) -> None:
        restored.append(clone_url)

    monkeypatch.setattr(git_repo_fetcher, "_run", fake_run)
    monkeypatch.setattr(git_repo_fetcher, "_restore_remote_url_sync", fake_restore)

    pr = _pr()
    await GitRepoFetcher(cache_dir=tmp_path)._checkout_locked(  # noqa: SLF001
        pr,
        "installation-token",
    )

    all_args = "\n".join(" ".join(cmd) for cmd, _env in calls)
    assert "installation-token" not in all_args
    assert "x-access-token:" not in all_args
    assert ["git", "clone", "--filter=blob:none", pr.clone_url, str(repo_path)] in [
        cmd for cmd, _env in calls
    ]

    clone_env = calls[0][1]
    fetch_env = next(env for cmd, env in calls if "fetch" in cmd)
    assert clone_env is not None
    assert fetch_env is not None
    assert clone_env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert clone_env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert "installation-token" not in clone_env["GIT_CONFIG_VALUE_0"]
    assert fetch_env["GIT_CONFIG_KEY_0"] == clone_env["GIT_CONFIG_KEY_0"]
    assert restored == [pr.clone_url]


@pytest.mark.asyncio
async def test_run_times_out_and_reaps_process(monkeypatch: pytest.MonkeyPatch) -> None:
    class HangingProcess:
        returncode = None
        pid = 1234

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(60)
            return b"", b""

    process = HangingProcess()
    reaped: list[HangingProcess] = []

    async def fake_create_subprocess_exec(
        *args: str,
        stdout: object,
        stderr: object,
        env: object,
    ) -> HangingProcess:
        return process

    async def fake_kill_and_reap(proc: HangingProcess) -> None:
        reaped.append(proc)

    monkeypatch.setattr(
        git_repo_fetcher.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(git_repo_fetcher, "kill_and_reap", fake_kill_and_reap)

    with pytest.raises(RuntimeError, match="git command timed out after 0.01s"):
        await git_repo_fetcher._run(["git", "status"], timeout_sec=0.01)  # noqa: SLF001

    assert reaped == [process]
