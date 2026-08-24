from __future__ import annotations

import asyncio
import fcntl
import os
import stat
from pathlib import Path

from ox_alpha_review.application.ports import StateSafetyError


class ExclusiveProcessLock:
    """OS-backed singleton lock for the v0.1 review and completion process."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    async def acquire(self) -> None:
        await asyncio.to_thread(self._acquire_sync)

    async def release(self) -> None:
        await asyncio.to_thread(self._release_sync)

    def _acquire_sync(self) -> None:
        if self._fd is not None:
            raise StateSafetyError("process lock is already held by this instance")
        fd: int | None = None
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self._path.parent, 0o700)
            flags = os.O_CREAT | os.O_RDWR
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self._path, flags, 0o600)
            os.chmod(self._path, 0o600)
            mode = os.fstat(fd).st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
                raise StateSafetyError("process lock file is not a mode-0600 regular file")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(fd)
            self._fd = fd
        except BlockingIOError as exc:
            if fd is not None:
                os.close(fd)
            raise StateSafetyError("another ox-alpha-review process is already running") from exc
        except StateSafetyError:
            if fd is not None:
                os.close(fd)
            raise
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            raise StateSafetyError("exclusive process lock could not be acquired") from exc

    def _release_sync(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
