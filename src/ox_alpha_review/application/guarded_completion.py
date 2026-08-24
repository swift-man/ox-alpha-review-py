from __future__ import annotations

from ox_alpha_review.application.ports import (
    Clock,
    CompletionAttemptJournal,
    CompletionTransport,
    FreeQuotaLedger,
    IdentifierFactory,
    KeyMetadataReader,
    ModelCatalogReader,
    OpenRouterTransportError,
    ProductionReadiness,
    SafetyLatch,
    StateSafetyError,
)
from ox_alpha_review.domain import CompletionPayload, CompletionResponse, FreeOnlyPolicy
from ox_alpha_review.domain.free_only import SafetyViolation


class GuardedCompletion:
    """The sole application path permitted to reach the completion transport."""

    def __init__(
        self,
        *,
        key_reader: KeyMetadataReader,
        catalog_reader: ModelCatalogReader,
        quota: FreeQuotaLedger,
        attempts: CompletionAttemptJournal,
        latch: SafetyLatch,
        readiness: ProductionReadiness,
        transport: CompletionTransport,
        policy: FreeOnlyPolicy,
        clock: Clock,
        identifiers: IdentifierFactory,
        key_fingerprint: str,
    ) -> None:
        self._key_reader = key_reader
        self._catalog_reader = catalog_reader
        self._quota = quota
        self._attempts = attempts
        self._latch = latch
        self._readiness = readiness
        self._transport = transport
        self._policy = policy
        self._clock = clock
        self._identifiers = identifiers
        self._key_fingerprint = key_fingerprint
        self._terminal_state_error: StateSafetyError | None = None

    async def complete(self, payload: CompletionPayload) -> CompletionResponse:
        if self._terminal_state_error is not None:
            raise self._terminal_state_error
        await self._latch.assert_clear()
        await self._attempts.assert_clean()
        await self._readiness.assert_ready(self._key_fingerprint)

        try:
            key = await self._key_reader.read()
            self._policy.validate_key(key)
            model, endpoints = await self._catalog_reader.read()
            self._policy.validate_catalog(model, endpoints)
        except SafetyViolation as exc:
            await self._trip_latch(str(exc))
            raise
        except OpenRouterTransportError as exc:
            if exc.status_code == 402:
                await self._trip_latch("OpenRouter returned HTTP 402 during safety preflight")
            raise

        reservation_id = self._identifiers.new()
        reservation_time = self._clock.now()
        try:
            await self._quota.reserve(reservation_id, reservation_time)
        except StateSafetyError as exc:
            await self._trip_latch("persistent quota state became unsafe")
            raise exc
        try:
            await self._attempts.begin(reservation_id, reservation_time)
        except StateSafetyError as exc:
            await self._trip_latch("completion attempt journal became unsafe")
            raise exc

        try:
            response = await self._transport.complete(payload)
        except SafetyViolation as exc:
            await self._trip_latch(str(exc))
            raise
        except OpenRouterTransportError as exc:
            if exc.context_limit and exc.pre_inference_rejection:
                await self._verify_attempt(reservation_id)
            elif exc.context_limit:
                await self._trip_latch(
                    "context-limit response lacked verified pre-inference rejection metadata"
                )
                raise OpenRouterTransportError(
                    "OpenRouter completion outcome could not be verified",
                    status_code=exc.status_code,
                ) from exc
            elif exc.status_code == 402:
                await self._trip_latch("OpenRouter returned HTTP 402")
            else:
                await self._trip_latch("completion outcome could not be verified")
            raise

        try:
            self._policy.validate_response(response)
        except SafetyViolation as exc:
            await self._trip_latch(str(exc))
            raise
        await self._verify_attempt(reservation_id)
        return response

    async def _verify_attempt(self, reservation_id: str) -> None:
        try:
            await self._attempts.verify(reservation_id)
        except StateSafetyError as exc:
            await self._trip_latch("completion attempt verification failed")
            raise exc

    async def _trip_latch(self, reason: str) -> None:
        try:
            await self._latch.trip(reason, self._clock.now())
        except StateSafetyError as exc:
            # A transient persistence failure must not let the same process attempt
            # another completion after observing a billing or identity violation.
            # The concrete latch also keeps its own fail-closed marker so readiness
            # checks are closed even when they do not go through this use case.
            terminal = StateSafetyError(
                "safety latch persistence failed; process remains fail-closed"
            )
            self._terminal_state_error = terminal
            raise terminal from exc
