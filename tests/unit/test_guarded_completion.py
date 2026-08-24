from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ox_alpha_review.application.guarded_completion import GuardedCompletion
from ox_alpha_review.application.ports import (
    OpenRouterTransportError,
    QuotaExceededError,
    ReadinessError,
    StateSafetyError,
)
from ox_alpha_review.domain import (
    CompletionPayload,
    CompletionResponse,
    EndpointMetadata,
    FreeOnlyPolicy,
    KeyMetadata,
    ModelMetadata,
    SafetyViolation,
)


class _KeyReader:
    def __init__(self, key: KeyMetadata, error: Exception | None = None) -> None:
        self.key = key
        self.error = error
        self.calls = 0

    async def read(self) -> KeyMetadata:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.key


class _CatalogReader:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def read(self) -> tuple[ModelMetadata, tuple[EndpointMetadata, ...]]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return (
            ModelMetadata(
                "stealth/ox-alpha",
                "stealth/ox-alpha",
                {"prompt": "0", "completion": "0"},
            ),
            (
                EndpointMetadata(
                    "stealth/ox-alpha",
                    "Stealth",
                    "stealth",
                    {"prompt": "0", "completion": "0", "discount": 0},
                    0,
                ),
            ),
        )


class _Quota:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0
        self.last_now: datetime | None = None

    async def reserve(self, reservation_id: str, now: datetime) -> int:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.last_now is not None and now < self.last_now:
            raise StateSafetyError("system clock moved backwards")
        self.last_now = now
        return 44


class _Attempts:
    def __init__(
        self,
        *,
        begin_error: Exception | None = None,
        verify_error: Exception | None = None,
    ) -> None:
        self.begin_error = begin_error
        self.verify_error = verify_error
        self.active: set[str] = set()
        self.clean_calls = 0
        self.begin_calls = 0
        self.verify_calls = 0

    async def assert_clean(self) -> None:
        self.clean_calls += 1
        if self.active:
            raise StateSafetyError("unverified attempt")

    async def begin(self, reservation_id: str, now: datetime) -> None:
        self.begin_calls += 1
        if self.begin_error is not None:
            raise self.begin_error
        self.active.add(reservation_id)

    async def verify(self, reservation_id: str) -> None:
        self.verify_calls += 1
        if self.verify_error is not None:
            raise self.verify_error
        self.active.remove(reservation_id)


class _Latch:
    def __init__(self, trip_error: Exception | None = None) -> None:
        self.trip_error = trip_error
        self.trips: list[str] = []

    async def assert_clear(self) -> None:
        return None

    async def trip(self, reason: str, now: datetime) -> None:
        self.trips.append(reason)
        if self.trip_error is not None:
            raise self.trip_error


class _Readiness:
    def __init__(self, ready: bool = True) -> None:
        self.ready = ready
        self.calls = 0

    async def assert_ready(self, key_fingerprint: str) -> None:
        self.calls += 1
        if not self.ready:
            raise ReadinessError("not accepted")

    async def status(self, key_fingerprint: str) -> tuple[bool, str]:
        return self.ready, "ready" if self.ready else "not accepted"


class _Transport:
    def __init__(
        self,
        response: CompletionResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response or CompletionResponse(
            "{}",
            "stealth/ox-alpha",
            "Stealth",
            Decimal("0"),
            {"upstream_inference_cost": 0},
            "default",
        )
        self.error = error
        self.calls = 0

    async def complete(self, payload: CompletionPayload) -> CompletionResponse:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


class _Clock:
    def __init__(self, *values: datetime) -> None:
        self.values = list(values) or [datetime(2026, 8, 24, tzinfo=UTC)]
        self.calls = 0

    def now(self) -> datetime:
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return value


class _Ids:
    def new(self) -> str:
        return "reservation"


def _key(**changes: object) -> KeyMetadata:
    values: dict[str, object] = {
        "is_free_tier": True,
        "is_management_key": False,
        "is_provisioning_key": False,
        "include_byok_in_limit": False,
        "spending_limit": None,
        "limit_remaining": None,
        "limit_reset": None,
        "usage": Decimal("0"),
        "byok_usage": Decimal("0"),
    }
    values.update(changes)
    return KeyMetadata(**values)  # type: ignore[arg-type]


def _guard(
    *,
    key: KeyMetadata | None = None,
    key_error: Exception | None = None,
    catalog_error: Exception | None = None,
    ready: bool = True,
    quota_error: Exception | None = None,
    response: CompletionResponse | None = None,
    transport_error: Exception | None = None,
    latch_trip_error: Exception | None = None,
    attempts: _Attempts | None = None,
    clock: _Clock | None = None,
) -> tuple[GuardedCompletion, _KeyReader, _CatalogReader, _Quota, _Latch, _Transport]:
    key_reader = _KeyReader(key or _key(), key_error)
    catalog = _CatalogReader(catalog_error)
    quota = _Quota(quota_error)
    latch = _Latch(latch_trip_error)
    transport = _Transport(response, transport_error)
    guarded = GuardedCompletion(
        key_reader=key_reader,
        catalog_reader=catalog,
        quota=quota,
        attempts=attempts or _Attempts(),
        latch=latch,
        readiness=_Readiness(ready),
        transport=transport,
        policy=FreeOnlyPolicy(),
        clock=clock or _Clock(),
        identifiers=_Ids(),
        key_fingerprint="fingerprint",
    )
    return guarded, key_reader, catalog, quota, latch, transport


def _payload() -> CompletionPayload:
    return CompletionPayload(
        messages=({"role": "user", "content": "review"},),
        max_tokens=1024,
    )


@pytest.mark.asyncio
async def test_not_ready_makes_zero_preflight_quota_and_transport_calls() -> None:
    guarded, key, catalog, quota, latch, transport = _guard(ready=False)

    with pytest.raises(ReadinessError):
        await guarded.complete(_payload())

    assert (key.calls, catalog.calls, quota.calls, transport.calls) == (0, 0, 0, 0)
    assert latch.trips == []


@pytest.mark.asyncio
async def test_key_guard_failure_trips_latch_with_zero_transport_calls() -> None:
    guarded, _, _, quota, latch, transport = _guard(key=_key(spending_limit=Decimal("1")))

    with pytest.raises(SafetyViolation):
        await guarded.complete(_payload())

    assert quota.calls == 0
    assert transport.calls == 0
    assert len(latch.trips) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("preflight", ["key", "catalog"])
async def test_preflight_http_402_trips_latch_with_zero_transport_calls(
    preflight: str,
) -> None:
    error = OpenRouterTransportError("payment", status_code=402)
    guarded, key, catalog, quota, latch, transport = _guard(
        key_error=error if preflight == "key" else None,
        catalog_error=error if preflight == "catalog" else None,
    )

    with pytest.raises(OpenRouterTransportError):
        await guarded.complete(_payload())

    assert key.calls == 1
    assert catalog.calls == (1 if preflight == "catalog" else 0)
    assert quota.calls == 0
    assert transport.calls == 0
    assert latch.trips == ["OpenRouter returned HTTP 402 during safety preflight"]


@pytest.mark.asyncio
async def test_quota_exhaustion_makes_zero_transport_calls_without_refund() -> None:
    guarded, _, _, quota, latch, transport = _guard(quota_error=QuotaExceededError("full"))

    with pytest.raises(QuotaExceededError):
        await guarded.complete(_payload())

    assert quota.calls == 1
    assert transport.calls == 0
    assert latch.trips == []


@pytest.mark.asyncio
async def test_corrupt_quota_state_trips_latch_before_transport() -> None:
    guarded, _, _, quota, latch, transport = _guard(quota_error=StateSafetyError("corrupt"))

    with pytest.raises(StateSafetyError):
        await guarded.complete(_payload())

    assert quota.calls == 1
    assert transport.calls == 0
    assert latch.trips == ["persistent quota state became unsafe"]


@pytest.mark.asyncio
async def test_http_402_is_not_retried_and_trips_latch() -> None:
    guarded, _, _, quota, latch, transport = _guard(
        transport_error=OpenRouterTransportError("payment", status_code=402)
    )

    with pytest.raises(OpenRouterTransportError):
        await guarded.complete(_payload())

    assert quota.calls == 1
    assert transport.calls == 1
    assert latch.trips == ["OpenRouter returned HTTP 402"]


@pytest.mark.asyncio
async def test_context_limit_closes_attempt_without_tripping_latch() -> None:
    attempts = _Attempts()
    guarded, _, _, quota, latch, transport = _guard(
        transport_error=OpenRouterTransportError(
            "maximum context length",
            status_code=400,
            context_limit=True,
            pre_inference_rejection=True,
        ),
        attempts=attempts,
    )

    with pytest.raises(OpenRouterTransportError):
        await guarded.complete(_payload())

    assert quota.calls == 1
    assert transport.calls == 1
    assert attempts.active == set()
    assert attempts.verify_calls == 1
    assert latch.trips == []


@pytest.mark.asyncio
async def test_unverified_context_limit_keeps_attempt_and_trips_latch() -> None:
    attempts = _Attempts()
    guarded, _, _, quota, latch, transport = _guard(
        transport_error=OpenRouterTransportError(
            "maximum context length",
            status_code=400,
            context_limit=True,
        ),
        attempts=attempts,
    )

    with pytest.raises(OpenRouterTransportError) as captured:
        await guarded.complete(_payload())

    assert captured.value.context_limit is False
    assert quota.calls == 1
    assert transport.calls == 1
    assert attempts.active == {"reservation"}
    assert attempts.verify_calls == 0
    assert latch.trips == [
        "context-limit response lacked verified pre-inference rejection metadata"
    ]


@pytest.mark.asyncio
async def test_attempt_journal_failure_trips_latch_before_transport() -> None:
    attempts = _Attempts(begin_error=StateSafetyError("disk unavailable"))
    guarded, _, _, quota, latch, transport = _guard(attempts=attempts)

    with pytest.raises(StateSafetyError, match="disk unavailable"):
        await guarded.complete(_payload())

    assert quota.calls == 1
    assert attempts.begin_calls == 1
    assert transport.calls == 0
    assert latch.trips == ["completion attempt journal became unsafe"]


@pytest.mark.asyncio
async def test_response_identity_mismatch_trips_latch_after_one_attempt() -> None:
    guarded, _, _, quota, latch, transport = _guard(
        response=CompletionResponse(
            "{}",
            "paid/model",
            "Stealth",
            Decimal("0"),
            {"upstream_inference_cost": 0},
            "default",
        )
    )

    with pytest.raises(SafetyViolation):
        await guarded.complete(_payload())

    assert quota.calls == 1
    assert transport.calls == 1
    assert len(latch.trips) == 1


@pytest.mark.asyncio
async def test_latch_persistence_failure_blocks_later_completion_in_process() -> None:
    guarded, key, catalog, quota, _, transport = _guard(
        response=CompletionResponse(
            "{}",
            "paid/model",
            "Stealth",
            Decimal("0"),
            {"upstream_inference_cost": 0},
            "default",
        ),
        latch_trip_error=StateSafetyError("disk unavailable"),
    )

    with pytest.raises(StateSafetyError, match="process remains fail-closed"):
        await guarded.complete(_payload())
    with pytest.raises(StateSafetyError, match="process remains fail-closed"):
        await guarded.complete(_payload())

    assert (key.calls, catalog.calls, quota.calls, transport.calls) == (1, 1, 1, 1)


@pytest.mark.asyncio
async def test_success_uses_exactly_one_reserved_attempt() -> None:
    attempts = _Attempts()
    guarded, key, catalog, quota, latch, transport = _guard(attempts=attempts)

    response = await guarded.complete(_payload())

    assert response.content == "{}"
    assert (key.calls, catalog.calls, quota.calls, transport.calls) == (1, 1, 1, 1)
    assert (attempts.begin_calls, attempts.verify_calls, attempts.active) == (1, 1, set())
    assert latch.trips == []


@pytest.mark.asyncio
async def test_clock_rollback_on_next_attempt_fails_before_second_transport() -> None:
    start = datetime(2026, 8, 24, tzinfo=UTC)
    clock = _Clock(start, start - timedelta(microseconds=1))
    guarded, _, _, quota, latch, transport = _guard(clock=clock)

    await guarded.complete(_payload())
    with pytest.raises(StateSafetyError, match="backwards"):
        await guarded.complete(_payload())

    assert quota.calls == 2
    assert transport.calls == 1
    assert latch.trips == ["persistent quota state became unsafe"]
