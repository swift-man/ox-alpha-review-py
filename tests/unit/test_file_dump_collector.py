import asyncio
from pathlib import Path

import pytest

from reviewbot_common.domain import ReviewPathFilter, TokenBudget
from reviewbot_common.infrastructure import file_dump_collector
from reviewbot_common.infrastructure.file_dump_collector import _build_dump_sync


def test_build_dump_skips_tracked_symlink_targets(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (repo / "leak.txt").symlink_to(outside)

    dump = _build_dump_sync(
        repo,
        ["leak.txt"],
        ("leak.txt",),
        {"leak.txt"},
        TokenBudget(max_tokens=10_000),
        file_max_bytes=100_000,
        data_file_max_bytes=100_000,
        path_filter=ReviewPathFilter.allow_all(),
    )

    assert dump.entries == ()
    assert dump.filter_excluded == ("leak.txt",)


def test_important_config_bypasses_data_limit_but_not_file_limit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    package_json = repo / "package.json"
    package_json.write_text('{"dependencies": "' + ("a" * 40) + '"}\n', encoding="utf-8")

    included = _build_dump_sync(
        repo,
        ["package.json"],
        ("package.json",),
        {"package.json"},
        TokenBudget(max_tokens=10_000),
        file_max_bytes=200,
        data_file_max_bytes=10,
        path_filter=ReviewPathFilter.allow_all(),
    )

    assert [entry.path for entry in included.entries] == ["package.json"]

    excluded = _build_dump_sync(
        repo,
        ["package.json"],
        ("package.json",),
        {"package.json"},
        TokenBudget(max_tokens=10_000),
        file_max_bytes=10,
        data_file_max_bytes=10,
        path_filter=ReviewPathFilter.allow_all(),
    )

    assert excluded.entries == ()
    assert excluded.filter_excluded == ("package.json",)


def test_always_review_bypasses_data_limit_but_not_file_limit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / "large.json"
    config.write_text('{"value": "' + ("a" * 40) + '"}\n', encoding="utf-8")
    path_filter = ReviewPathFilter(always_review=("large.json",))

    included = _build_dump_sync(
        repo,
        ["large.json"],
        ("large.json",),
        {"large.json"},
        TokenBudget(max_tokens=10_000),
        file_max_bytes=200,
        data_file_max_bytes=10,
        path_filter=path_filter,
    )

    assert [entry.path for entry in included.entries] == ["large.json"]

    excluded = _build_dump_sync(
        repo,
        ["large.json"],
        ("large.json",),
        {"large.json"},
        TokenBudget(max_tokens=10_000),
        file_max_bytes=10,
        data_file_max_bytes=10,
        path_filter=path_filter,
    )

    assert excluded.entries == ()
    assert excluded.filter_excluded == ("large.json",)


@pytest.mark.asyncio
async def test_git_ls_files_times_out_and_reaps_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class HangingProcess:
        returncode = None
        pid = 4321

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
        file_dump_collector.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(file_dump_collector, "kill_and_reap", fake_kill_and_reap)

    with pytest.raises(RuntimeError, match="git ls-files timed out after 0.01s"):
        await file_dump_collector._git_ls_files(  # noqa: SLF001
            tmp_path,
            timeout_sec=0.01,
        )

    assert reaped == [process]
