import os
from pathlib import Path

import pytest

from ox_alpha_review.application.ports import StateSafetyError
from ox_alpha_review.infrastructure.process_lock import ExclusiveProcessLock


@pytest.mark.asyncio
async def test_process_lock_rejects_second_instance_until_release(tmp_path: Path) -> None:
    path = tmp_path / "state" / "safety.sqlite3.lock"
    first = ExclusiveProcessLock(path)
    second = ExclusiveProcessLock(path)

    await first.acquire()
    try:
        with pytest.raises(StateSafetyError, match="already running"):
            await second.acquire()
    finally:
        await first.release()

    await second.acquire()
    await second.release()
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_process_lock_never_chmods_the_path_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state" / "safety.sqlite3.lock"
    original_chmod = os.chmod

    def reject_path_chmod(target: os.PathLike[str] | str, mode: int) -> None:
        if Path(target) == path:
            raise AssertionError("opened lock file must be secured through its descriptor")
        original_chmod(target, mode)

    monkeypatch.setattr(os, "chmod", reject_path_chmod)
    lock = ExclusiveProcessLock(path)

    await lock.acquire()
    await lock.release()

    assert path.stat().st_mode & 0o777 == 0o600
