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
