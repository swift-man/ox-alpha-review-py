from pathlib import Path
from typing import Protocol

from reviewbot_common.domain import FileDump, TokenBudget


class FileCollector(Protocol):
    async def collect(
        self,
        root: Path,
        changed_files: tuple[str, ...],
        budget: TokenBudget,
    ) -> FileDump: ...
