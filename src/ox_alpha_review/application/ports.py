from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ox_alpha_review.domain import (
    CompletionPayload,
    CompletionResponse,
    EndpointMetadata,
    KeyMetadata,
    ModelMetadata,
)


class StateSafetyError(RuntimeError):
    """Persistent safety state could not be trusted."""


class SafetyLatchTrippedError(StateSafetyError):
    """A previously persisted safety violation keeps inference disabled."""


class QuotaExceededError(RuntimeError):
    pass


class ReadinessError(RuntimeError):
    pass


class OpenRouterTransportError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        context_limit: bool = False,
        pre_inference_rejection: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.context_limit = context_limit
        self.pre_inference_rejection = pre_inference_rejection


class KeyMetadataReader(Protocol):
    async def read(self) -> KeyMetadata: ...


class ModelCatalogReader(Protocol):
    async def read(self) -> tuple[ModelMetadata, tuple[EndpointMetadata, ...]]: ...


class FreeQuotaLedger(Protocol):
    async def reserve(self, reservation_id: str, now: datetime) -> int: ...


class CompletionAttemptJournal(Protocol):
    async def assert_clean(self) -> None: ...

    async def begin(self, reservation_id: str, now: datetime) -> None: ...

    async def verify(self, reservation_id: str) -> None: ...


class SafetyLatch(Protocol):
    async def assert_clear(self) -> None: ...

    async def trip(self, reason: str, now: datetime) -> None: ...


class ProductionReadiness(Protocol):
    async def assert_ready(self, key_fingerprint: str) -> None: ...

    async def status(self, key_fingerprint: str) -> tuple[bool, str]: ...


class CompletionTransport(Protocol):
    async def complete(self, payload: CompletionPayload) -> CompletionResponse: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdentifierFactory(Protocol):
    def new(self) -> str: ...
