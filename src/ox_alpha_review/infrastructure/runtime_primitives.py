from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class UuidFactory:
    def new(self) -> str:
        return str(uuid4())
